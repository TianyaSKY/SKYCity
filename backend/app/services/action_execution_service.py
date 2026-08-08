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
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.gameplay import (
    CAPACITY_RECHECK_MINUTES,
    DEFAULT_WAIT_MINUTES,
    HOTEL_LOCATION_ID,
    HOTEL_NIGHTLY_FEE,
    MINUTES_PER_STEP,
    WEATHER_MULTIPLIERS,
)
from app.database.models.agents import Agent
from app.database.models.companies import Company, CompanyTransaction
from app.database.models.locations import WorldLocation
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.transactions import Transaction
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

# R1 rejection messages.
MSG_PAUSED = "世界已暂停"
MSG_BUSY = "当前行动未完成"
MSG_NO_DESTINATION = "目标地点不存在"
MSG_NO_PATH = "无可行路径"
MSG_START_BLOCKED = "所在位置被建筑挡住，无法出发"
MSG_AGENT_MISSING = "智能体不存在"
MSG_WORLD_MISSING = "世界不存在"

# Sleep place rule (R14): agents with a home sleep at home; homeless agents
# sleep at the hotel, paying HOTEL_NIGHTLY_FEE (R7: no credit).
MSG_SLEEP_NEED_HOME = "有家必须回家睡觉（当前不在家）"
MSG_SLEEP_NEED_HOTEL = "没有家的智能体需要去小镇旅店睡觉"
MSG_HOTEL_UNAFFORDABLE = f"余额不足，付不起旅店房费（每晚 {HOTEL_NIGHTLY_FEE} 金币）"

Importance = "normal"


def _line_clear(
        a: tuple[int, int],
        b: tuple[int, int],
        walkable_cells: set[tuple[int, int]] | frozenset[tuple[int, int]],
) -> bool:
    """True when the straight 8-directional run a->b visits only walkable
    cells (endpoints excluded — they already sit on the path). Mirrors the
    BFS step rule: a hop is legal iff its landing cell is walkable."""
    steps = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
    if steps <= 1:
        return True
    for k in range(1, steps):
        col = round(a[0] + (b[0] - a[0]) * k / steps)
        row = round(a[1] + (b[1] - a[1]) * k / steps)
        if (col, row) not in walkable_cells:
            return False
    return True


def _smooth_path(
        path: list[tuple[int, int]],
        walkable_cells: set[tuple[int, int]] | frozenset[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Greedy string pulling: collapse BFS stair-steps into long straight
    runs whenever the chord stays on walkable cells. Preserves start/goal."""
    if len(path) <= 2:
        return path
    result: list[tuple[int, int]] = [path[0]]
    i = 0
    while i < len(path) - 1:
        j = i + 1
        while j + 1 < len(path) and _line_clear(path[i], path[j + 1], walkable_cells):
            j += 1
        result.append(path[j])
        i = j
    return result


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
            # R6 duration stays tied to the raw BFS step count (the game-time
            # cost model); the emitted route is the string-pulled waypoint
            # list so clients render clean lines instead of BFS stair-steps.
            route = _smooth_path(path, walkable)
            steps = max(len(path) - 1, 0)
            multiplier = WEATHER_MULTIPLIERS.get(world.weather, 1.0)
            duration = int(steps * MINUTES_PER_STEP * multiplier)
            ends_at = world.world_time + duration
            agent.action_type = "move"
            agent.action_started_at = world.world_time
            agent.action_ends_at = ends_at
            agent.action_data = {
                "from": list(start),
                "to": list(goal),
                "path": [list(cell) for cell in route],
                "reason": reason,
            }
            runtime.scheduler.schedule(
                session,
                agent_id,
                "move_completed",
                ends_at,
                {
                    "destination_id": destination_id,
                    "destination_name": destination.name,
                    "duration_minutes": duration,
                    "steps": steps,
                },
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "agent_move_started",
                {
                    "agent_id": agent_id,
                    "from": list(start),
                    "to": list(goal),
                    "path": [list(cell) for cell in route],
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

        Sleep place rule (R14): an agent with a home must sleep at that home;
        a homeless agent must sleep at the hotel (village_hotel), which
        charges HOTEL_NIGHTLY_FEE per night on start (R7: no credit).
        Sleep recovers energy/mood much faster than wait (rates live in
        app.config.gameplay); the engine tick keys recovery off
        action_type == "sleep".
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
            if agent.action_type == "move":
                return False, None, MSG_BUSY
            # Sleep place validation happens BEFORE the wait/sleep replacement
            # below so a rejected sleep never destroys an in-flight wait.
            home_id = self.engine.home_location_id(agent_id)
            if home_id is not None:
                if agent.location_id != home_id:
                    return False, None, MSG_SLEEP_NEED_HOME
                fee = 0
            else:
                if agent.location_id != HOTEL_LOCATION_ID:
                    return False, None, MSG_SLEEP_NEED_HOTEL
                fee = HOTEL_NIGHTLY_FEE
                if agent.money < fee:
                    return False, None, MSG_HOTEL_UNAFFORDABLE  # R7
            if agent.action_type is not None:
                # R1: wait/sleep is interruptible -> cancel pending + replace.
                runtime.scheduler.cancel_for_agent(session, agent_id)
                self._clear_action(agent)
            sleep_minutes = minutes if minutes is not None else DEFAULT_WAIT_MINUTES
            ends_at = world.world_time + sleep_minutes
            agent.action_type = "sleep"
            agent.action_started_at = world.world_time
            agent.action_ends_at = ends_at
            agent.action_data = {"reason": reason, "place": agent.location_id}
            if fee > 0:
                agent.money -= fee
                session.add(
                    Transaction(
                        world_id=world_id,
                        agent_id=agent_id,
                        type="hotel_fee",
                        amount=-fee,
                        balance_after=agent.money,
                        item_id=None,
                        quantity=None,
                        reason="旅店住宿费",
                        world_time=world.world_time,
                        trace_id=trace_id or "",
                    )
                )
                runtime.event_bus.publish(
                    session,
                    world.world_time,
                    "money_changed",
                    {
                        "agent_id": agent_id,
                        "amount": -fee,
                        "balance": agent.money,
                        "reason": "旅店住宿费",
                    },
                    trace_id,
                )
                # M18: the night fee lands in the hotel company's treasury
                # (mirrors the R33 shop-sale income path), so the hotel can
                # pay staff and its manager sees a real revenue stream.
                hotel = session.scalar(
                    select(Company).where(
                        Company.world_id == world_id,
                        Company.location_id == HOTEL_LOCATION_ID,
                    )
                )
                if hotel is not None:
                    hotel.money += fee
                    session.add(
                        CompanyTransaction(
                            world_id=world_id,
                            company_id=hotel.company_id,
                            type="hotel_income",
                            amount=fee,
                            balance_after=hotel.money,
                            related_agent_id=agent_id,
                            reference_type="sleep",
                            reason="旅店住宿费",
                            world_time=world.world_time,
                            trace_id=trace_id or "",
                        )
                    )
                    runtime.event_bus.publish(
                        session,
                        world.world_time,
                        "company_money_changed",
                        {
                            "company_id": hotel.company_id,
                            "amount": fee,
                            "balance": hotel.money,
                            "reason": "旅店住宿费",
                        },
                        trace_id,
                    )
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
                    "place": agent.location_id,
                    "fee": fee,
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
            {
                "agent_id": action.agent_id,
                "at": [destination.col, destination.row],
                "destination_id": destination.location_id,
                "destination_name": destination.name,
                "duration_minutes": payload.get("duration_minutes", 0),
            },
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
