"""ActionExecutionService: validates and executes agent actions (R1/R6/R8/R15).

This is the world engine's rule gate (world-rules.md). It is the only place
world rules are enforced; the HTTP layer only passes intent. Every accepted
action writes an event envelope that is returned to the caller and queued for
WebSocket delivery.

Handlers registered on each world's Scheduler complete actions at their due
world_time (move_completed, wait_completed, capacity_recheck).
"""

from __future__ import annotations

from collections import deque

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.locations import WorldLocation
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.worlds import World
from app.domain.event import WorldEventEnvelope
from app.schemas.actions import ActionRequest
from app.world_engine.engine import (
    WorldEngine,
    WorldRuntime,
    count_location_occupants,
    is_location_open,
    next_open_time,
)

# R6: game minutes per grid step by weather.
WEATHER_MULTIPLIERS = {"clear": 1.0, "cloudy": 1.0, "rain": 1.5, "snow": 2.0}
MINUTES_PER_STEP = 2

# R1 rejection messages.
MSG_PAUSED = "世界已暂停"
MSG_BUSY = "当前行动未完成"
MSG_NO_DESTINATION = "目标地点不存在"
MSG_NO_PATH = "无可行路径"
MSG_START_BLOCKED = "所在位置被建筑挡住，无法出发"
MSG_AGENT_MISSING = "智能体不存在"
MSG_WORLD_MISSING = "世界不存在"

# R15: capacity re-check interval (minutes) while waiting for a free slot.
CAPACITY_RECHECK_MINUTES = 30

# Default wait duration when the API omits minutes.
DEFAULT_WAIT_MINUTES = 60

Importance = "normal"


def find_path(
    start: tuple[int, int],
    goal: tuple[int, int],
    walkable_cells: set[tuple[int, int]] | frozenset[tuple[int, int]],
) -> list[tuple[int, int]] | None:
    """BFS over walkable cells from ``start`` to ``goal`` (inclusive).

    The walkable network is 8-connected (map generator places spawn points and
    location anchors diagonally adjacent to the road cells), so movement allows
    the 8 compass directions. The goal cell is treated as passable even if it
    carries no navigation marker (location anchors sit on building tiles).
    Returns None when no path exists.
    """
    if start == goal:
        return [start]
    frontier: deque[tuple[int, int]] = deque([start])
    came_from = {start: None}
    while frontier:
        current = frontier.popleft()
        if current == goal:
            break
        col, row = current
        for dcol in (-1, 0, 1):
            for drow in (-1, 0, 1):
                if dcol == 0 and drow == 0:
                    continue
                neighbour = (col + dcol, row + drow)
                if neighbour in came_from:
                    continue
                if neighbour != goal and neighbour not in walkable_cells:
                    continue
                came_from[neighbour] = current
                frontier.append(neighbour)
    if goal not in came_from:
        return None
    path: list[tuple[int, int]] = [goal]
    while path[-1] != start:
        prev = came_from[path[-1]]
        if prev is None:
            return None
        path.append(prev)
    path.reverse()
    return path


class ActionExecutionService:
    """Executes agent actions against world rules and schedules completions."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # Entry point used by the HTTP layer
    # ------------------------------------------------------------------ #

    def execute_action(
        self, world_id: str, agent_id: str, request: ActionRequest, trace_id: str | None = None
    ) -> tuple[bool, WorldEventEnvelope | None, str | None]:
        """Dispatch a validated action request. Returns (ok, envelope, reason)."""
        if request.action_type == "move":
            return self.execute_move(world_id, agent_id, request.destination_id, request.reason, trace_id)
        if request.action_type == "talk":
            conversation_service = self.engine.conversation_service
            if conversation_service is None:
                return False, None, "对话服务未初始化"
            ok, reason, envelope = conversation_service.send_message(
                world_id,
                agent_id,
                request.target_agent_id,
                request.message,
                request.intent,
                trace_id,
            )
            return ok, envelope, reason
        # M5 economy actions.
        economy_service = self.engine.economy_service
        if economy_service is None:
            return False, None, "经济服务未初始化"
        if request.action_type == "work":
            return economy_service.work_start(
                world_id, agent_id, request.job_id, reason=request.reason, trace_id=trace_id
            )
        if request.action_type == "buy_item":
            return economy_service.buy(
                world_id,
                agent_id,
                request.item_id,
                quantity=request.quantity if request.quantity is not None else 1,
                reason=request.reason,
                trace_id=trace_id,
            )
        if request.action_type == "sell_item":
            return economy_service.sell(
                world_id,
                agent_id,
                request.item_id,
                quantity=request.quantity if request.quantity is not None else 1,
                reason=request.reason,
                trace_id=trace_id,
            )
        if request.action_type == "use_item":
            return economy_service.use_item(
                world_id, agent_id, request.item_id, reason=request.reason, trace_id=trace_id
            )
        # M10 stock actions through the stock rule gate.
        if request.action_type in ("buy_stock", "sell_stock"):
            stock_service = self.engine.stock_service
            if stock_service is None:
                return False, None, "股票服务未初始化"
            fn = (
                stock_service.buy_stock
                if request.action_type == "buy_stock"
                else stock_service.sell_stock
            )
            return fn(
                world_id,
                agent_id,
                request.stock_id,
                shares=request.shares if request.shares is not None else 1,
                reason=request.reason,
                trace_id=trace_id,
            )
        # M11 agent-to-agent transfer / give.
        transfer_service = self.engine.transfer_service
        if request.action_type in ("transfer_money", "give_item"):
            if transfer_service is None:
                return False, None, "转账服务未初始化"
            if request.action_type == "transfer_money":
                return transfer_service.transfer_money(
                    world_id, agent_id, request.target_agent_id,
                    amount=request.amount or 1, reason=request.reason, trace_id=trace_id,
                )
            return transfer_service.give_item(
                world_id, agent_id, request.target_agent_id, request.item_id,
                quantity=request.quantity if request.quantity is not None else 1,
                reason=request.reason, trace_id=trace_id,
            )
        if request.action_type == "sleep":
            return self.execute_sleep(
                world_id, agent_id, request.minutes, request.reason, trace_id=trace_id
            )
        # M14 build through the construction rule gate (R22).
        if request.action_type == "build":
            build_service = self.engine.build_service
            if build_service is None:
                return False, None, "建造服务未初始化"
            return build_service.build_start(
                world_id,
                agent_id,
                request.col,
                request.row,
                request.blueprint_id,
                reason=request.reason,
                trace_id=trace_id,
            )
        # M15 plant/harvest through the farming rule gate (R23).
        if request.action_type in ("plant", "harvest"):
            crop_service = self.engine.crop_service
            if crop_service is None:
                return False, None, "种植服务未初始化"
            if request.action_type == "plant":
                return crop_service.plant(
                    world_id,
                    agent_id,
                    request.col,
                    request.row,
                    request.item_id,
                    reason=request.reason,
                    trace_id=trace_id,
                )
            return crop_service.harvest(
                world_id,
                agent_id,
                request.col,
                request.row,
                reason=request.reason,
                trace_id=trace_id,
            )
        return self.execute_wait(world_id, agent_id, request.minutes, request.reason, trace_id)

    # ------------------------------------------------------------------ #
    # Manual actions
    # ------------------------------------------------------------------ #

    def execute_move(
        self,
        world_id: str,
        agent_id: str,
        destination_id: str | None,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[bool, WorldEventEnvelope | None, str | None]:
        """Validate + start a move (R1/R6/R8-start/R15-at-completion/paused)."""
        session = self._session_factory()
        try:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return False, None, MSG_AGENT_MISSING
            if agent.action_type is not None:
                if agent.action_type == "move":
                    # R1: move is exclusive; a second move is rejected.
                    return False, None, MSG_BUSY
                # R1: wait is interruptible -> cancel pending and replace it.
                runtime.scheduler.cancel_for_agent(session, agent_id)
                self._clear_action(agent)
            if not destination_id:
                return False, None, MSG_NO_DESTINATION
            destination = session.get(
                WorldLocation, {"world_id": world_id, "location_id": destination_id}
            )
            if destination is None:
                return False, None, MSG_NO_DESTINATION
            start = (agent.col, agent.row)
            goal = (destination.col, destination.row)
            # R22.6: pathfinding runs on effective_walkable — blocking built
            # structures are real obstacles. A structure built on the agent's
            # own cell blocks departure; location anchors (not in the static
            # walkable set) are unaffected.
            walkable = self.engine.effective_walkable(session, world_id)
            if (
                start in self.engine.world_config.walkable_cells
                and start not in walkable
            ):
                return False, None, MSG_START_BLOCKED
            path = find_path(start, goal, walkable)
            if path is None:
                return False, None, MSG_NO_PATH
            steps = max(len(path) - 1, 0)
            multiplier = WEATHER_MULTIPLIERS.get(world.weather, 1.0)
            duration = int(steps * MINUTES_PER_STEP * multiplier)
            ends_at = world.world_time + duration
            agent.action_type = "move"
            agent.action_started_at = world.world_time
            agent.action_ends_at = ends_at
            agent.action_data = {"from": list(start), "to": list(goal), "reason": reason}
            runtime.scheduler.schedule(
                session,
                agent_id,
                "move_completed",
                ends_at,
                {"destination_id": destination_id, "destination_name": destination.name},
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "agent_move_started",
                {
                    "agent_id": agent_id,
                    "from": list(start),
                    "to": list(goal),
                    "duration_minutes": duration,
                    "speed_multiplier": multiplier,
                    "ends_at": ends_at,
                    "reason": reason,
                },
                trace_id,
            )
            session.commit()
            return True, envelope, None
        finally:
            session.close()

    def execute_wait(
        self,
        world_id: str,
        agent_id: str,
        minutes: int | None = None,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[bool, WorldEventEnvelope | None, str | None]:
        """Validate + start a wait (R1: wait is interruptible)."""
        session = self._session_factory()
        try:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return False, None, MSG_AGENT_MISSING
            if agent.action_type is not None:
                if agent.action_type == "move":
                    return False, None, MSG_BUSY
                runtime.scheduler.cancel_for_agent(session, agent_id)
                self._clear_action(agent)
            wait_minutes = minutes if minutes is not None else DEFAULT_WAIT_MINUTES
            ends_at = world.world_time + wait_minutes
            agent.action_type = "wait"
            agent.action_started_at = world.world_time
            agent.action_ends_at = ends_at
            agent.action_data = {"reason": reason}
            runtime.scheduler.schedule(
                session, agent_id, "wait_completed", ends_at, {"reason": reason}
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "agent_wait_started",
                {
                    "agent_id": agent_id,
                    "minutes": wait_minutes,
                    "ends_at": ends_at,
                    "reason": reason,
                },
                trace_id,
            )
            session.commit()
            return True, envelope, None
        finally:
            session.close()

    def execute_sleep(
        self,
        world_id: str,
        agent_id: str,
        minutes: int | None = None,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[bool, WorldEventEnvelope | None, str | None]:
        """Validate + start a sleep (R1: interruptible like wait).

        Sleep recovers energy much faster than wait (R14: +20/h vs +5/h);
        the engine tick keys recovery off action_type == "sleep".
        """
        session = self._session_factory()
        try:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return False, None, MSG_AGENT_MISSING
            if agent.action_type is not None:
                if agent.action_type == "move":
                    return False, None, MSG_BUSY
                runtime.scheduler.cancel_for_agent(session, agent_id)
                self._clear_action(agent)
            sleep_minutes = minutes if minutes is not None else DEFAULT_WAIT_MINUTES
            ends_at = world.world_time + sleep_minutes
            agent.action_type = "sleep"
            agent.action_started_at = world.world_time
            agent.action_ends_at = ends_at
            agent.action_data = {"reason": reason}
            runtime.scheduler.schedule(
                session, agent_id, "sleep_completed", ends_at, {"reason": reason}
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "agent_sleep_started",
                {
                    "agent_id": agent_id,
                    "minutes": sleep_minutes,
                    "ends_at": ends_at,
                    "reason": reason,
                },
                trace_id,
            )
            session.commit()
            return True, envelope, None
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Scheduler handlers (run on the engine tick at due world_time)
    # ------------------------------------------------------------------ #

    def handle_move_completed(self, session: Session, action: ScheduledAction) -> None:
        """Arrival: snap to the destination anchor, then R8/R15 follow-ups."""
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        agent = session.get(Agent, {"world_id": action.world_id, "agent_id": action.agent_id})
        if agent is None or agent.action_type != "move":
            return  # stale or already replaced
        payload = action.payload or {}
        destination = session.get(
            WorldLocation,
            {"world_id": action.world_id, "location_id": payload.get("destination_id")},
        )
        if destination is None:
            self._clear_action(agent)
            return
        world_time = runtime.clock.world_time
        agent.col = destination.col
        agent.row = destination.row
        self._clear_action(agent)
        runtime.event_bus.publish(
            session,
            world_time,
            "agent_move_completed",
            {"agent_id": action.agent_id, "at": [destination.col, destination.row]},
        )

        # M4 R9: moving out of earshot ends the mover's active conversations.
        # Commit first so a separate session sees the new position.
        session.commit()
        if self.engine.conversation_service is not None:
            self.engine.conversation_service.end_if_distance_exceeded(
                action.world_id, action.agent_id
            )

        if not is_location_open(destination.location_type, destination.open_hour, destination.close_hour, world_time):
            # R8: allowed to walk there, but the venue is shut -> wait at the door.
            open_at = next_open_time(destination.open_hour, destination.close_hour, world_time)
            minutes = max(open_at - world_time, 1)
            agent.location_id = destination.location_id
            runtime.event_bus.publish(
                session,
                world_time,
                "world_event_created",
                {
                    "agent_id": action.agent_id,
                    "text": f"{agent.name} 到了 {destination.name}，但 {destination.name} 还没开门",
                    "importance": Importance,
                },
            )
            self._start_wait(
                session, runtime, agent, minutes, f"等待{destination.name}开门", open_at
            )
            return

        occupants = count_location_occupants(session, action.world_id, destination.location_id)
        if occupants >= destination.capacity:
            # R15: full at arrival -> wait by the door and re-check later.
            agent.location_id = destination.location_id
            runtime.event_bus.publish(
                session,
                world_time,
                "world_event_created",
                {
                    "agent_id": action.agent_id,
                    "text": f"{agent.name} 到了 {destination.name}，但 {destination.name} 已满，等待空位",
                    "importance": Importance,
                },
            )
            self._start_wait(
                session,
                runtime,
                agent,
                CAPACITY_RECHECK_MINUTES,
                f"等待{destination.name}空位",
                recheck_destination_id=destination.location_id,
            )
            return

        agent.location_id = destination.location_id
        self._maybe_schedule_next_decision(session, action)

    def handle_wait_completed(self, session: Session, action: ScheduledAction) -> None:
        """A wait finished: agent is idle again."""
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        agent = session.get(Agent, {"world_id": action.world_id, "agent_id": action.agent_id})
        if agent is None or agent.action_type != "wait":
            return
        self._clear_action(agent)
        runtime.event_bus.publish(
            session,
            runtime.clock.world_time,
            "agent_wait_completed",
            {"agent_id": action.agent_id, "at": [agent.col, agent.row]},
        )
        self._maybe_schedule_next_decision(session, action)

    def handle_sleep_completed(self, session: Session, action: ScheduledAction) -> None:
        """A sleep finished: agent is awake and idle again."""
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        agent = session.get(Agent, {"world_id": action.world_id, "agent_id": action.agent_id})
        if agent is None or agent.action_type != "sleep":
            return
        self._clear_action(agent)
        runtime.event_bus.publish(
            session,
            runtime.clock.world_time,
            "agent_sleep_completed",
            {"agent_id": action.agent_id, "at": [agent.col, agent.row]},
        )
        self._maybe_schedule_next_decision(session, action)

    def handle_capacity_recheck(self, session: Session, action: ScheduledAction) -> None:
        """R15 re-evaluation: enter if a slot freed, otherwise wait again."""
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        agent = session.get(Agent, {"world_id": action.world_id, "agent_id": action.agent_id})
        if agent is None or agent.action_type != "wait":
            return
        destination = session.get(
            WorldLocation,
            {"world_id": action.world_id, "location_id": (action.payload or {}).get("destination_id")},
        )
        world_time = runtime.clock.world_time
        if destination is not None and (
            count_location_occupants(session, action.world_id, destination.location_id)
            < destination.capacity
        ):
            agent.location_id = destination.location_id
            self._clear_action(agent)
            runtime.event_bus.publish(
                session,
                world_time,
                "agent_wait_completed",
                {"agent_id": action.agent_id, "at": [agent.col, agent.row]},
            )
            self._maybe_schedule_next_decision(session, action)
            return
        # Still full: keep waiting and schedule another check.
        new_end = world_time + CAPACITY_RECHECK_MINUTES
        agent.action_ends_at = new_end
        runtime.scheduler.schedule(
            session,
            action.agent_id,
            "capacity_recheck",
            new_end,
            {"destination_id": destination.location_id if destination else None},
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _maybe_schedule_next_decision(
        self, session: Session, action: ScheduledAction
    ) -> None:
        """M3: autonomous worlds re-arm the LLM loop when an action completes.

        Only when the agent is idle again (a completion handler may have
        chained a door wait / capacity wait instead) and not mid-decision.
        """
        if self.engine.decision_service is None:
            return
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        world = session.get(World, action.world_id)
        if world is None or not world.autonomous or world.paused:
            return
        agent = session.get(Agent, {"world_id": action.world_id, "agent_id": action.agent_id})
        if agent is None or agent.action_type is not None or agent.is_deciding:
            return
        runtime.scheduler.schedule(
            session,
            action.agent_id,
            "agent_decide",
            runtime.clock.world_time,
            {"origin": "completed"},
        )

    def _start_wait(
        self,
        session: Session,
        runtime: WorldRuntime,
        agent: Agent,
        minutes: int,
        reason: str,
        wait_until: int | None = None,
        recheck_destination_id: str | None = None,
    ) -> None:
        """Begin an auto-scheduled wait (R8 door wait or R15 capacity wait)."""
        ends_at = wait_until if wait_until is not None else runtime.clock.world_time + minutes
        agent.action_type = "wait"
        agent.action_started_at = runtime.clock.world_time
        agent.action_ends_at = ends_at
        agent.action_data = {"reason": reason}
        if recheck_destination_id is not None:
            runtime.scheduler.schedule(
                session,
                agent.agent_id,
                "capacity_recheck",
                ends_at,
                {"destination_id": recheck_destination_id},
            )
        else:
            runtime.scheduler.schedule(session, agent.agent_id, "wait_completed", ends_at, {"reason": reason})
        runtime.event_bus.publish(
            session,
            runtime.clock.world_time,
            "agent_wait_started",
            {
                "agent_id": agent.agent_id,
                "minutes": minutes,
                "ends_at": ends_at,
                "reason": reason,
            },
        )

    @staticmethod
    def _clear_action(agent: Agent) -> None:
        agent.action_type = None
        agent.action_started_at = None
        agent.action_ends_at = None
        agent.action_data = None

    def log_rejection(self, world_id: str, agent_id: str, reason: str) -> None:  # pragma: no cover
        logger.debug("Action rejected world={} agent={}: {}", world_id, agent_id, reason)
