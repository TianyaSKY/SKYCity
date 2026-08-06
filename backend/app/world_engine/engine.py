"""WorldEngine: runtime registry, tick loop, world lifecycle, snapshots, WS fan-out.

One WorldRuntime (clock + scheduler + event bus + ws clients) per world_id.
The engine is created in the FastAPI lifespan; its asyncio task ticks every
0.1 real seconds. All DB work inside the tick loop is sync SQLite (fast).

Sync DB mutations in HTTP handlers run on the same event loop via async
endpoints; WebSocket pushes are queued by the EventBus and drained by
``flush_pending`` (async) after each mutation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.websockets import WebSocket

from app.database.models.agents import (
    Agent,
    INITIAL_LONELINESS,
    INITIAL_MOOD,
    INITIAL_SATIETY,
)
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.jobs import Job
from app.database.models.locations import WorldLocation
from app.database.models.stores import Store, StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.world_events import WorldEvent
from app.database.models.worlds import World
from app.domain.agent import AgentActionMove, AgentActionWait, AgentActionWork, AgentSnapshot
from app.domain.event import WorldEventEnvelope
from app.domain.location import LocationSnapshot
from app.domain.world import WorldSnapshot, WorldSnapshotPayload
from app.services.memory_service import MemoryRecorder, MemoryService
from app.services.relationship_service import RelationshipService
from app.services.seed_loader import load_items, load_jobs, load_stores
from app.services.world_config_loader import ParsedWorldConfig
from app.world_engine.clock import WorldClock
from app.world_engine.event_bus import EventBus
from app.world_engine.scheduler import Scheduler

TICK_INTERVAL = 0.1  # real seconds per engine tick

# M12 D6: daily cost of living, deducted at 00:00 (floor 0, never debt).
UPKEEP_PER_DAY = 5

_HOME_SUFFIX = "_home"


def _promo_roll(world_id: str, store_id: str, item_id: str, day: int) -> bool:
    """M12 D5: deterministic 20% chance of a promo day for one product.

    Hash-based (not random) so ``advance_minutes`` fast-forwards in tests
    reproduce exactly the same promo set as a live run.
    """
    digest = hashlib.md5(f"{world_id}:{store_id}:{item_id}:{day}".encode()).hexdigest()
    return int(digest[:8], 16) % 10 < 2


@dataclass
class WorldRuntime:
    """In-memory state for one running world."""

    world_id: str
    clock: WorldClock
    scheduler: Scheduler
    event_bus: EventBus
    ws_clients: set[WebSocket] = field(default_factory=set)
    # M5: hour index of the last applied hourly needs tick (idempotency guard).
    last_hour: int | None = None
    # M8: day index of the last tick (daily counter reset guard).
    last_day: int | None = None


class WorldEngine:
    """Registry of world runtimes + the background tick loop."""

    def __init__(
        self,
        session_factory: sessionmaker,
        world_config: ParsedWorldConfig,
        world_data_dir: Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.world_config = world_config
        self.world_data_dir = world_data_dir
        self._runtimes: dict[str, WorldRuntime] = {}
        self._task: asyncio.Task | None = None
        # ActionExecutionService is wired after construction (it needs the engine).
        self.action_service: Any = None
        # DecisionService (M3) is wired after construction; when set, the
        # "agent_decide" scheduler handler is registered per runtime.
        self.decision_service: Any = None
        # ConversationService (M4) is wired after construction; the talk tool,
        # the decision service, and the move_completed handler reach it here.
        self.conversation_service: Any = None
        # EconomyService (M5) is wired after construction; the work/buy/sell/
        # use tools and the "work_completed" scheduler handler reach it here.
        self.economy_service: Any = None
        # GodActionService (M7) is wired after construction; the god-actions
        # REST endpoint reaches it here.
        self.god_action_service: Any = None
        # StockService (M10) is wired after construction; trading tools, the
        # stocks REST endpoint and the hourly/daily market ticks reach it here.
        self.stock_service: Any = None
        # TransferService (M11) is wired after construction; transfer/give tools reach it here.
        self.transfer_service: Any = None
        # M6: memory + relationship services are self-contained (engine +
        # session factory), so they are constructed here and active in every
        # engine — memory recording and relationship deltas are automatic.
        self.memory_service = MemoryService(self, session_factory)
        self.memory_recorder = MemoryRecorder(self, session_factory, self.memory_service)
        self.relationship_service = RelationshipService(self, session_factory)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._task = asyncio.create_task(self._tick_loop(), name="world-engine-tick")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def load_existing(self) -> None:
        """Restore runtimes for worlds persisted in the DB (restart-safe)."""
        session = self._session_factory()
        try:
            worlds = session.scalars(select(World).order_by(World.world_id)).all()
            for world in worlds:
                runtime = self._ensure_runtime(
                    world.world_id,
                    clock=WorldClock(world.world_time, world.speed, world.paused),
                    session=session,
                )
                # M6: restore the once-per-day reflection arm (restart-safe).
                self.memory_service.ensure_daily_reflection_scheduled(
                    session, runtime, world.world_time
                )
                if world.autonomous and not world.paused:
                    # Restart-safe: re-arm the decision loop for idle agents.
                    self._schedule_idle_decisions(session, runtime, world, delay=2)
            if worlds:
                session.commit()
                logger.info(
                    "World engine resumed {} existing world(s)", len(worlds)
                )
        except Exception:  # noqa: BLE001 - DB may be empty/migrating; engine still works
            logger.exception("Failed to resume existing worlds; starting empty")
        finally:
            session.close()

    def runtime_ids(self) -> list[str]:
        return sorted(self._runtimes)

    def get_runtime(self, world_id: str) -> WorldRuntime | None:
        return self._runtimes.get(world_id)

    def idle_agents_near(
        self, world_id: str, agent_id: str, distance: int
    ) -> list[str]:
        """Other agents that are idle (no action in flight) and within
        manhattan ``distance`` cells of ``agent_id``, nearest first.

        Powers the fake provider's conversation initiation (M4 demo): an idle
        agent at a busy spot picks the closest idle neighbour to greet.
        """
        session = self._session_factory()
        try:
            me = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if me is None:
                return []
            others = session.scalars(
                select(Agent).where(
                    Agent.world_id == world_id,
                    Agent.agent_id != agent_id,
                    Agent.action_type.is_(None),
                )
            ).all()
            scored = [
                (abs(me.col - other.col) + abs(me.row - other.row), other.agent_id)
                for other in others
                if abs(me.col - other.col) + abs(me.row - other.row) <= distance
            ]
            scored.sort(key=lambda item: (item[0], item[1]))
            return [agent_id for _, agent_id in scored]
        finally:
            session.close()

    def waiting_agents_near(
        self, world_id: str, agent_id: str, distance: int
    ) -> list[tuple[str, int]]:
        """(agent_id, remaining_minutes) for other agents currently waiting
        (action_type == "wait") within manhattan ``distance`` cells, sorted by
        when they free up.

        Powers the fake provider's conversation convergence (M4 demo): an idle
        agent whose only nearby neighbour is waiting pauses until that
        neighbour's wait ends, so both become idle together and can talk.
        """
        session = self._session_factory()
        try:
            me = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if me is None:
                return []
            world_time = int(
                session.scalar(
                    select(World.world_time).where(World.world_id == world_id)
                )
                or 0
            )
            others = session.scalars(
                select(Agent).where(
                    Agent.world_id == world_id,
                    Agent.agent_id != agent_id,
                    Agent.action_type == "wait",
                )
            ).all()
            scored: list[tuple[int, str, int]] = []
            for other in others:
                if abs(me.col - other.col) + abs(me.row - other.row) > distance:
                    continue
                remaining = max((other.action_ends_at or world_time) - world_time, 0)
                scored.append((remaining, other.agent_id, remaining))
            scored.sort(key=lambda item: (item[0], item[1]))
            return [(agent_id, remaining) for _, agent_id, remaining in scored]
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Runtime construction
    # ------------------------------------------------------------------ #

    def _ensure_runtime(
        self, world_id: str, clock: WorldClock | None = None, session: Session | None = None
    ) -> WorldRuntime:
        existing = self._runtimes.get(world_id)
        if existing is not None:
            return existing
        if self.action_service is None:
            raise RuntimeError("WorldEngine.action_service must be wired before runtimes are created")
        event_bus = EventBus(world_id)
        scheduler = Scheduler(world_id)
        if session is not None:
            event_bus.init_sequence(session)
        # M6: every published envelope feeds memory recording + relationship
        # deltas (derived memory_created/relationship_changed events are
        # filtered out by the services, so the hook terminates).
        event_bus.on_publish = self._on_event_hooks
        runtime = WorldRuntime(
            world_id=world_id,
            clock=clock or WorldClock(),
            scheduler=scheduler,
            event_bus=event_bus,
        )
        scheduler.register("move_completed", self.action_service.handle_move_completed)
        scheduler.register("wait_completed", self.action_service.handle_wait_completed)
        scheduler.register("sleep_completed", self.action_service.handle_sleep_completed)
        scheduler.register("capacity_recheck", self.action_service.handle_capacity_recheck)
        if self.economy_service is not None:
            scheduler.register("work_completed", self.economy_service.handle_work_completed)
        if self.decision_service is not None:
            scheduler.register("agent_decide", self.decision_service.handle_agent_decide)
        scheduler.register("daily_reflection", self.memory_service.handle_daily_reflection)
        self._runtimes[world_id] = runtime
        return runtime

    def _on_event_hooks(self, session: Session, envelope: WorldEventEnvelope) -> None:
        """M6: derive memories + relationship deltas from every published event.

        Runs inside the publisher's transaction (the derived rows commit with
        the source event). One failing hook must never break the world, so
        each service is guarded independently.
        """
        # M8 T8-3: important events boost idle agents' next decision.
        try:
            self._maybe_decision_boost(session, envelope)
        except Exception:  # noqa: BLE001 - a boost bug must not kill the tick
            logger.exception(
                "Decision boost failed on event {} (world={})",
                envelope.type,
                envelope.world_id,
            )
        if self.memory_recorder is not None:
            try:
                self.memory_recorder.on_event(session, envelope)
            except Exception:  # noqa: BLE001 - memory bug must not kill the tick
                logger.exception(
                    "Memory recorder failed on event {} (world={})",
                    envelope.type,
                    envelope.world_id,
                )
        if self.relationship_service is not None:
            try:
                self.relationship_service.on_event(session, envelope)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Relationship service failed on event {} (world={})",
                    envelope.type,
                    envelope.world_id,
                )
        # M10: 经营事件 → 股价 (business events bump the listed company's price).
        if self.stock_service is not None:
            try:
                self.stock_service.on_event(session, envelope)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Stock service failed on event {} (world={})",
                    envelope.type,
                    envelope.world_id,
                )

    def _maybe_decision_boost(
        self, session: Session, envelope: WorldEventEnvelope
    ) -> None:
        """M8 T8-3: important events wake idle agents sooner.

        - ``god_action_applied`` with a target: that agent decides at +1.
        - public ``world_event_created`` (no agent_id): every idle agent
          decides at +3, staggered by agent index (no burst).
        """
        world = session.get(World, envelope.world_id)
        if world is None or not world.autonomous:
            return
        runtime = self._runtimes.get(envelope.world_id)
        if runtime is None:
            return
        payload = envelope.payload or {}
        if envelope.type == "god_action_applied":
            target = payload.get("target_id")
            if target:
                agent = session.get(
                    Agent, {"world_id": envelope.world_id, "agent_id": target}
                )
                if agent is not None and agent.action_type is None:
                    runtime.scheduler.schedule(
                        session,
                        target,
                        "agent_decide",
                        envelope.world_time + 1,
                        {"origin": "god_boost"},
                    )
        elif envelope.type == "world_event_created" and not payload.get("agent_id"):
            agents = session.scalars(
                select(Agent)
                .where(
                    Agent.world_id == envelope.world_id,
                    Agent.action_type.is_(None),
                )
                .order_by(Agent.agent_id)
            ).all()
            for index, agent in enumerate(agents):
                runtime.scheduler.schedule(
                    session,
                    agent.agent_id,
                    "agent_decide",
                    envelope.world_time + 3 + index,
                    {"origin": "event_boost"},
                )

    # ------------------------------------------------------------------ #
    # World lifecycle
    # ------------------------------------------------------------------ #

    def create_world(
        self, name: str | None = None, autonomous: bool = False
    ) -> WorldRuntime:
        """Create a new world seeded with locations + agents, return its runtime.

        With ``autonomous=True`` the world joins the LLM decision loop: each
        agent gets an initial agent_decide scheduled (staggered).
        """
        session = self._session_factory()
        try:
            number = self._next_world_number(session)
            world_id = f"world_{number:03d}"
            world = World(
                world_id=world_id,
                name=name or f"世界 {number:03d}",
                world_time=480,
                speed=1,
                paused=False,
                weather="clear",
                autonomous=autonomous,
            )
            session.add(world)
            for loc in self.world_config.locations:
                session.add(
                    WorldLocation(
                        world_id=world_id,
                        location_id=loc.location_id,
                        name=loc.name,
                        location_type=loc.location_type,
                        col=loc.col,
                        row=loc.row,
                        capacity=loc.capacity,
                        open_hour=loc.open_hour,
                        close_hour=loc.close_hour,
                    )
                )
            for spawn in self.world_config.spawn_points:
                session.add(self._build_agent(world_id, spawn))
            self._seed_economy(session, world_id)
            session.commit()
        finally:
            session.close()
        # A world id is unique; a stale in-memory runtime (e.g. after the
        # DB was reset) must never leak its sequence counter into the new world.
        self._runtimes.pop(world_id, None)
        runtime = self._ensure_runtime(world_id, clock=WorldClock(480, 1, False))
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            # M6: arm the once-per-day 23:30 reflection (T6-6).
            self.memory_service.ensure_daily_reflection_scheduled(
                session, runtime, world.world_time
            )
            if autonomous:
                self._schedule_initial_decisions(session, runtime, world, base_delay=2)
            session.commit()
        finally:
            session.close()
        logger.info("Created world {} (autonomous={})", world_id, autonomous)
        return runtime

    def delete_runtime(self, world_id: str) -> None:
        """Remove an in-memory runtime (no DB delete in M2)."""
        self._runtimes.pop(world_id, None)

    def delete_world(self, world_id: str) -> bool:
        """Permanently delete a world and all its state.

        Removes the runtime first (in-flight decision tasks guard on
        ``get_runtime`` returning None and exit cleanly); the DB row delete
        cascades to every child table (agents, events, llm_runs, saves, …).
        Returns False when the world does not exist.

        The cascade runs on a dedicated raw connection with foreign_keys=ON.
        A pooled session connection must NOT be used: the pragma is
        per-connection and survives the pool checkout, so ORM sessions that
        later reuse the connection would enforce FKs and break their flush
        order (pure-FK mappers insert agents before worlds).
        """
        self.delete_runtime(world_id)
        session = self._session_factory()
        try:
            bind = session.get_bind()
        finally:
            session.close()
        raw = bind.raw_connection()
        try:
            cursor = raw.cursor()
            # PRAGMA before any transaction starts (and again after commit,
            # so the connection returns to the pool with FKs OFF).
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("SELECT 1 FROM worlds WHERE world_id = ?", (world_id,))
            if cursor.fetchone() is None:
                raw.rollback()
                return False
            cursor.execute("DELETE FROM worlds WHERE world_id = ?", (world_id,))
            raw.commit()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()
            logger.info("Deleted world {}", world_id)
            return True
        finally:
            try:
                raw.execute("PRAGMA foreign_keys=OFF")
            except Exception:  # noqa: BLE001 - best-effort reset
                pass
            raw.close()

    def _next_world_number(self, session: Session) -> int:
        ids = session.scalars(select(World.world_id)).all()
        numbers = [
            int(parts[1])
            for world_id in ids
            if (parts := world_id.split("_", 1)) and len(parts) == 2 and parts[1].isdigit()
        ]
        return (max(numbers) if numbers else 0) + 1

    def _build_agent(self, world_id: str, spawn: Any) -> Agent:
        identity = self._load_identity(spawn.agent_id)
        home_id = f"{spawn.agent_id.removeprefix('agent_')}{_HOME_SUFFIX}"
        home_exists = any(loc.location_id == home_id for loc in self.world_config.locations)
        return Agent(
            world_id=world_id,
            agent_id=spawn.agent_id,
            name=identity.get("name") or spawn.agent_id,
            age=int(identity.get("age") or 0),
            occupation=str(identity.get("occupation") or ""),
            background=str(identity.get("background") or ""),
            values=list(identity.get("values") or []),
            long_term_goals=list(identity.get("long_term_goals") or []),
            speaking_style=str(identity.get("speaking_style") or ""),
            personality=dict(identity.get("personality") or {}),
            col=spawn.col,
            row=spawn.row,
            direction=spawn.direction,
            location_id=home_id if home_exists else None,
            satiety=INITIAL_SATIETY,
            energy=100,
            mood=INITIAL_MOOD,
            loneliness=INITIAL_LONELINESS,
            money=int(identity.get("initial_money") or 50),
            action_type=None,
            action_started_at=None,
            action_ends_at=None,
            action_data=None,
        )

    def _seed_economy(self, session: Session, world_id: str) -> None:
        """M5: seed per-world items, stores (+ products at full stock) and jobs.

        Employment rows are deliberately absent until the first completed work.
        """
        for seed in load_items(self.world_data_dir):
            session.add(
                Item(
                    world_id=world_id,
                    item_id=seed["item_id"],
                    name=seed["name"],
                    category=seed["category"],
                    satiety_restore=seed["satiety_restore"],
                    mood_restore=seed["mood_restore"],
                    work_bonus=seed["work_bonus"],
                    yield_bonus=seed["yield_bonus"],
                    base_price=seed["base_price"],
                )
            )
        for store_seed in load_stores(self.world_data_dir):
            session.add(
                Store(
                    world_id=world_id,
                    store_id=store_seed["store_id"],
                    location_id=store_seed["location_id"],
                )
            )
            for product in store_seed["products"]:
                session.add(
                    StoreProduct(
                        world_id=world_id,
                        store_id=store_seed["store_id"],
                        item_id=product["item_id"],
                        sell_price=product["sell_price"],
                        base_sell_price=product["sell_price"],
                        buy_price=product["buy_price"],
                        stock=product["stock_cap"],  # R15: full stock at open
                        stock_cap=product["stock_cap"],
                        restock_daily=product["restock_daily"],
                    )
                )
        for job_seed in load_jobs(self.world_data_dir):
            session.add(
                Job(
                    world_id=world_id,
                    job_id=job_seed["job_id"],
                    name=job_seed["name"],
                    location_id=job_seed["location_id"],
                    interactable_id=job_seed["interactable_id"],
                    duration_minutes=job_seed["duration_minutes"],
                    wage=job_seed["wage"],
                    energy_cost_per_hour=job_seed["energy_cost_per_hour"],
                    products_json=job_seed["products"],
                )
            )
        if self.stock_service is not None:
            self.stock_service.seed(session, world_id)

    def _load_identity(self, agent_id: str) -> dict[str, Any]:
        if self.world_data_dir is None:
            return {}
        path = self.world_data_dir / "identities" / f"{agent_id}.json"
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            logger.warning("Identity card missing: {}", path)
            return {}
        except json.JSONDecodeError:
            logger.warning("Identity card invalid JSON: {}", path)
            return {}

    # ------------------------------------------------------------------ #
    # Publish (used by HTTP routes; queues + persists, caller flushes)
    # ------------------------------------------------------------------ #

    def publish(
        self,
        world_id: str,
        type_: str,
        payload: dict | None = None,
        trace_id: str | None = None,
        world_time: int | None = None,
    ) -> WorldEventEnvelope | None:
        """Persist one event for a world; returns its envelope (None if unknown world)."""
        runtime = self._runtimes.get(world_id)
        if runtime is None:
            return None
        session = self._session_factory()
        try:
            if world_time is None:
                world_time = runtime.clock.world_time
            envelope = runtime.event_bus.publish(session, world_time, type_, payload, trace_id)
            session.commit()
            return envelope
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Control mutations (pause / resume / speed)
    # ------------------------------------------------------------------ #

    def set_paused(self, world_id: str, paused: bool) -> tuple[bool, WorldEventEnvelope | None]:
        """Pause/resume a world. Returns (found, envelope_or_None); envelope is
        None when the state did not change (idempotent call)."""
        runtime = self._runtimes.get(world_id)
        if runtime is None:
            return False, None
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            if world is None:
                return False, None
            if world.paused == paused:
                return True, None
            world.paused = paused
            runtime.clock.paused = paused
            if not paused and world.autonomous:
                # Resume: autonomous worlds re-arm the decision loop for idle agents.
                self._schedule_idle_decisions(session, runtime, world, delay=2)
            session.commit()
            envelope = self.publish(world_id, "world_paused" if paused else "world_resumed", {})
            return True, envelope
        finally:
            session.close()

    def set_speed(self, world_id: str, speed: int) -> tuple[bool, WorldEventEnvelope | None]:
        """Change world speed. Returns (found, envelope_or_None); envelope is
        None when the speed did not change."""
        runtime = self._runtimes.get(world_id)
        if runtime is None:
            return False, None
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            if world is None:
                return False, None
            if world.speed == speed:
                return True, None
            world.speed = speed
            runtime.clock.speed = speed
            session.commit()
            envelope = self.publish(world_id, "world_speed_changed", {"speed": speed})
            return True, envelope
        finally:
            session.close()

    def set_autonomous(self, world_id: str, enabled: bool) -> tuple[bool, bool]:
        """Enable/disable the LLM decision loop for a world.

        Returns (found, changed). Enabling schedules an initial agent_decide
        for every agent, staggered +2+i*1 game minutes.
        """
        runtime = self._runtimes.get(world_id)
        if runtime is None:
            return False, False
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            if world is None:
                return False, False
            if world.autonomous == enabled:
                return True, False
            world.autonomous = enabled
            if enabled:
                self._schedule_initial_decisions(session, runtime, world, base_delay=2)
            session.commit()
            logger.info("World {} autonomous={}", world_id, enabled)
            return True, True
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Decision-loop scheduling helpers (M3)
    # ------------------------------------------------------------------ #

    def _schedule_initial_decisions(
        self,
        session: Session,
        runtime: WorldRuntime,
        world: World,
        base_delay: int = 2,
    ) -> None:
        """Schedule the first agent_decide for every agent, staggered."""
        agents = session.scalars(
            select(Agent)
            .where(Agent.world_id == world.world_id)
            .order_by(Agent.agent_id)
        ).all()
        for index, agent in enumerate(agents):
            runtime.scheduler.schedule(
                session,
                agent.agent_id,
                "agent_decide",
                world.world_time + base_delay + index,
                {"origin": "autonomous"},
            )

    def _schedule_idle_decisions(
        self,
        session: Session,
        runtime: WorldRuntime,
        world: World,
        delay: int = 2,
    ) -> None:
        """Re-arm the decision loop for agents that are currently idle."""
        agents = session.scalars(
            select(Agent)
            .where(Agent.world_id == world.world_id, Agent.action_type.is_(None))
            .order_by(Agent.agent_id)
        ).all()
        for agent in agents:
            runtime.scheduler.schedule(
                session,
                agent.agent_id,
                "agent_decide",
                world.world_time + delay,
                {"origin": "resume"},
            )

    # ------------------------------------------------------------------ #
    # Tick loop
    # ------------------------------------------------------------------ #

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            for runtime in list(self._runtimes.values()):
                try:
                    self._tick_runtime(runtime)
                    await self._flush_pending(runtime)
                except Exception:  # noqa: BLE001 - one world must not kill the loop
                    logger.exception("Tick failed for world {}", runtime.world_id)

    def _tick_runtime(self, runtime: WorldRuntime) -> None:
        """Advance one world's clock and fire due scheduler actions.

        Frozen world (paused): neither clock nor scheduler run.
        """
        if runtime.clock.paused:
            return
        crossed = runtime.clock.tick(TICK_INTERVAL)
        if not crossed:
            return
        session = self._session_factory()
        try:
            world = session.get(World, runtime.world_id)
            if world is None:
                return
            for world_time in crossed:
                world.world_time = world_time
                runtime.event_bus.publish(
                    session, world_time, "world_time_changed", {"world_time": world_time}
                )
                self._maybe_hourly_tick(session, runtime, world, world_time)
            session.commit()
            due = runtime.scheduler.load_due(session, runtime.clock.world_time)
            for action in due:
                runtime.scheduler.dispatch(session, action)
            session.commit()
        finally:
            session.close()

    def _maybe_hourly_tick(
        self,
        session: Session,
        runtime: WorldRuntime,
        world: World,
        world_time: int,
    ) -> None:
        """M5 R14: apply the hourly needs rhythm exactly once per hour.

        The ``last_hour`` guard makes the tick idempotent per hour boundary;
        needs_changed events, R11/R12 decision boosts and R15 restocks all
        ride on this same crossing and share the caller's single commit.
        """
        hour = world_time // 60
        if runtime.last_hour is not None and hour != runtime.last_hour:
            self._apply_hourly_needs(session, runtime, world, world_time)
            self._maybe_restock(session, runtime, world, world_time)
            self._tick_stock_prices(session, runtime, world, world_time)
            # M8: daily counters reset at the day boundary (midnight crossing).
            day = world_time // 1440
            if runtime.last_day is not None and day != runtime.last_day:
                # M10: dividends settle before the daily counters reset (same
                # transaction, same commit). M12 D6: upkeep is deducted
                # between dividends and the counter reset.
                self._pay_dividends(session, runtime, world, world_time)
                self._apply_daily_upkeep(session, runtime, world, world_time)
                self._reset_daily_counters(session, world.world_id)
        runtime.last_hour = hour
        runtime.last_day = world_time // 1440

    def _tick_stock_prices(
        self,
        session: Session,
        runtime: WorldRuntime,
        world: World,
        world_time: int,
    ) -> None:
        """M10: hourly market tick (delegates to StockService when wired)."""
        if self.stock_service is not None:
            self.stock_service.tick_prices(session, runtime, world, world_time)

    def _pay_dividends(
        self,
        session: Session,
        runtime: WorldRuntime,
        world: World,
        world_time: int,
    ) -> None:
        """M10: daily dividend settlement at 00:00 (delegates when wired)."""
        if self.stock_service is not None:
            self.stock_service.pay_dividends(session, runtime, world, world_time)

    def _apply_daily_upkeep(
        self,
        session: Session,
        runtime: WorldRuntime,
        world: World,
        world_time: int,
    ) -> None:
        """M12 D6: daily cost of living at 00:00.

        Every agent pays UPKEEP_PER_DAY out of money (floor 0 — R7: no
        credit, never into debt). Recorded as an ``upkeep`` transaction plus
        a money_changed event.
        """
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world.world_id)
        ).all()
        for agent in agents:
            if agent.money <= 0:
                continue  # R7: never into debt
            pay = min(agent.money, UPKEEP_PER_DAY)
            agent.money -= pay
            session.add(
                Transaction(
                    world_id=world.world_id,
                    agent_id=agent.agent_id,
                    type="upkeep",
                    amount=-pay,
                    balance_after=agent.money,
                    item_id=None,
                    quantity=None,
                    reason="每日生活开销",
                    world_time=world_time,
                    trace_id="",
                )
            )
            runtime.event_bus.publish(
                session,
                world_time,
                "money_changed",
                {
                    "agent_id": agent.agent_id,
                    "amount": -pay,
                    "balance": agent.money,
                    "reason": "每日生活开销",
                },
            )

    def _reset_daily_counters(self, session: Session, world_id: str) -> None:
        """M8: zero every agent's daily LLM call/token counters (day change)."""
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world_id)
        ).all()
        for agent in agents:
            agent.daily_token_usage = 0
            agent.daily_call_count = 0

    def _apply_hourly_needs(
        self,
        session: Session,
        runtime: WorldRuntime,
        world: World,
        world_time: int,
    ) -> None:
        """R14 defaults: satiety -1/h, energy -1/h, wait +5/h, sleep +20/h,
        satiety==0 -1/h. M12: mood -1/h, wait +2/h, sleep +10/h.
        R21: loneliness +1/h, high loneliness boosts decisions."""
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world.world_id)
        ).all()
        for agent in agents:
            before = (agent.satiety, agent.energy, agent.mood, agent.loneliness)
            agent.satiety = max(0, agent.satiety - 1)
            agent.energy = max(0, agent.energy - 1)
            agent.mood = max(0, agent.mood - 1)
            agent.loneliness = min(100, agent.loneliness + 1)
            if agent.action_type == "wait":
                agent.energy = min(100, agent.energy + 5)
                agent.mood = min(100, agent.mood + 2)
            elif agent.action_type == "sleep":
                agent.energy = min(100, agent.energy + 20)
                agent.mood = min(100, agent.mood + 10)
            if agent.satiety <= 0:
                agent.energy = max(0, agent.energy - 1)  # R11 extra drain
            if (agent.satiety, agent.energy, agent.mood, agent.loneliness) != before:
                runtime.event_bus.publish(
                    session,
                    world_time,
                    "needs_changed",
                    {
                        "agent_id": agent.agent_id,
                        "satiety": agent.satiety,
                        "energy": agent.energy,
                        "mood": agent.mood,
                        "loneliness": agent.loneliness,
                    },
                )
            # R11/R12/M12/R21: satiety empty, energy drained, mood low or
            # loneliness high -> high-priority decision.
            if (
                world.autonomous
                and agent.action_type is None
                and (
                    agent.satiety <= 0
                    or agent.energy <= 0
                    or agent.mood <= 20
                    or agent.loneliness >= 80
                )
            ):
                runtime.scheduler.schedule(
                    session,
                    agent.agent_id,
                    "agent_decide",
                    world_time + 1,
                    {"origin": "needs_boost"},
                )

    def _maybe_restock(
        self,
        session: Session,
        runtime: WorldRuntime,
        world: World,
        world_time: int,
    ) -> None:
        """R15: at a store's daily open hour, restock toward stock_cap."""
        # Open hours live on the location the store covers (R8).
        stores = session.scalars(
            select(Store)
            .join(
                WorldLocation,
                (WorldLocation.world_id == Store.world_id)
                & (WorldLocation.location_id == Store.location_id),
            )
            .where(
                Store.world_id == world.world_id,
                WorldLocation.open_hour * 60 == world_time % 1440,
            )
        ).all()
        for store in stores:
            restocked: list[dict[str, Any]] = []
            products = session.scalars(
                select(StoreProduct).where(
                    StoreProduct.world_id == world.world_id,
                    StoreProduct.store_id == store.store_id,
                )
            ).all()
            for product in products:
                # M12 D5: daily promo roll — 20% off for the whole day,
                # otherwise back to the base price.
                day = world_time // 1440
                promo = _promo_roll(world.world_id, store.store_id, product.item_id, day)
                new_price = (
                    max(1, round(product.base_sell_price * 0.8))
                    if promo
                    else product.base_sell_price
                )
                if new_price != product.sell_price:
                    product.sell_price = new_price
                    item = session.get(
                        Item,
                        {"world_id": world.world_id, "item_id": product.item_id},
                    )
                    runtime.event_bus.publish(
                        session,
                        world_time,
                        "store_price_changed",
                        {
                            "store_id": store.store_id,
                            "item_id": product.item_id,
                            "item_name": item.name if item is not None else product.item_id,
                            "sell_price": new_price,
                            "promo": promo,
                        },
                    )
                target = min(product.stock_cap, product.stock + product.restock_daily)
                gained = target - product.stock
                if gained > 0:
                    product.stock = target
                    restocked.append({"item_id": product.item_id, "quantity": gained})
            if restocked:
                runtime.event_bus.publish(
                    session,
                    world_time,
                    "store_restocked",
                    {"store_id": store.store_id, "restocked": restocked},
                )

    async def _flush_pending(self, runtime: WorldRuntime) -> None:
        """Send every queued envelope to connected WebSocket clients."""
        envelopes = runtime.event_bus.take_pending()
        if not envelopes:
            return
        if not runtime.ws_clients:
            return
        dead: list[WebSocket] = []
        for client in list(runtime.ws_clients):
            try:
                for envelope in envelopes:
                    await client.send_json(envelope.model_dump())
            except Exception:  # noqa: BLE001 - broken client; drop it
                dead.append(client)
        for client in dead:
            runtime.ws_clients.discard(client)

    async def flush_pending_now(self, world_id: str) -> None:
        """Flush queued envelopes to WS clients (HTTP handlers, after a mutation)."""
        runtime = self._runtimes.get(world_id)
        if runtime is None:
            return
        await self._flush_pending(runtime)

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #

    def snapshot(self, world_id: str) -> dict[str, Any] | None:
        """Full world state payload (same shape WS sends as world_snapshot)."""
        runtime = self._runtimes.get(world_id)
        if runtime is None:
            return None
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            if world is None:
                return None
            agents = session.scalars(
                select(Agent).where(Agent.world_id == world_id).order_by(Agent.agent_id)
            ).all()
            locations = session.scalars(
                select(WorldLocation)
                .where(WorldLocation.world_id == world_id)
                .order_by(WorldLocation.location_id)
            ).all()
            world_time = world.world_time
            payload = WorldSnapshotPayload(
                world=WorldSnapshot(
                    world_id=world_id,
                    world_time=world_time,
                    speed=world.speed,
                    paused=world.paused,
                    weather=world.weather,
                    day=world_time // 1440 + 1,
                ),
                agents=[self._agent_snapshot(session, agent, world_time) for agent in agents],
                locations=[self._location_snapshot(loc, world_time) for loc in locations],
                latest_sequence=runtime.event_bus.sequence,
            )
            return payload.model_dump(by_alias=True)
        finally:
            session.close()

    def snapshot_envelope(self, world_id: str) -> dict[str, Any] | None:
        """The world_snapshot envelope sent on WS connect (not persisted)."""
        runtime = self._runtimes.get(world_id)
        if runtime is None:
            return None
        payload = self.snapshot(world_id)
        if payload is None:
            return None
        sequence = runtime.event_bus.sequence
        return {
            "event_id": f"evt_{sequence:06d}",
            "sequence": sequence,
            "world_id": world_id,
            "world_time": runtime.clock.world_time,
            "type": "world_snapshot",
            "payload": payload,
            "trace_id": f"trc_{sequence:06d}",
        }

    def _agent_snapshot(self, session: Session, agent: Agent, world_time: int) -> AgentSnapshot:
        action = None
        if agent.action_type == "move":
            data = agent.action_data or {}
            action = AgentActionMove(
                type="move",
                from_=list(data.get("from") or [agent.col, agent.row]),
                to=list(data.get("to") or [agent.col, agent.row]),
                started_at=agent.action_started_at or world_time,
                ends_at=agent.action_ends_at or world_time,
                reason=data.get("reason"),
            )
        elif agent.action_type == "wait":
            data = agent.action_data or {}
            action = AgentActionWait(
                type="wait",
                ends_at=agent.action_ends_at or world_time,
                reason=data.get("reason"),
            )
        elif agent.action_type == "work":
            data = agent.action_data or {}
            job = session.get(Job, {"world_id": agent.world_id, "job_id": data.get("job_id")})
            action = AgentActionWork(
                type="work",
                job_id=str(data.get("job_id") or ""),
                job_name=job.name if job is not None else None,
                started_at=agent.action_started_at or world_time,
                ends_at=agent.action_ends_at or world_time,
                reason=data.get("reason"),
            )
        inventory_rows = session.scalars(
            select(Inventory)
            .where(Inventory.world_id == agent.world_id, Inventory.agent_id == agent.agent_id)
            .order_by(Inventory.item_id)
        ).all()
        return AgentSnapshot(
            agent_id=agent.agent_id,
            name=agent.name,
            col=agent.col,
            row=agent.row,
            location_id=agent.location_id,
            satiety=agent.satiety,
            energy=agent.energy,
            mood=agent.mood,
            loneliness=agent.loneliness,
            money=agent.money,
            action=action,
            inventory=[
                {"item_id": row.item_id, "quantity": row.quantity} for row in inventory_rows
            ],
        )

    def agent_detail(self, world_id: str, agent_id: str) -> dict[str, Any] | None:
        """M7: one agent's detail — identity card + state + inventory + action.

        Contract shape: {agent_id, name, identity: {...}, col, row, location_id,
        satiety, energy, mood, loneliness, money, inventory, action,
        is_deciding, consecutive_failures}. Returns None when the world or
        agent is missing.
        """
        runtime = self._runtimes.get(world_id)
        if runtime is None:
            return None
        session = self._session_factory()
        try:
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return None
            snapshot = self._agent_snapshot(session, agent, runtime.clock.world_time)
            detail = snapshot.model_dump(by_alias=True)
            detail["identity"] = {
                "id": agent.agent_id,
                "name": agent.name,
                "age": agent.age,
                "occupation": agent.occupation,
                "background": agent.background,
                "values": agent.values or [],
                "long_term_goals": agent.long_term_goals or [],
                "speaking_style": agent.speaking_style,
                "personality": agent.personality or {},
            }
            detail["is_deciding"] = agent.is_deciding
            detail["consecutive_failures"] = agent.consecutive_failures
            return detail
        finally:
            session.close()

    def _location_snapshot(self, loc: WorldLocation, world_time: int) -> LocationSnapshot:
        return LocationSnapshot(
            location_id=loc.location_id,
            name=loc.name,
            location_type=loc.location_type,
            col=loc.col,
            row=loc.row,
            capacity=loc.capacity,
            open_hour=loc.open_hour,
            close_hour=loc.close_hour,
            open=is_location_open(loc.location_type, loc.open_hour, loc.close_hour, world_time),
        )

    def location_detail(self, world_id: str, location_id: str) -> dict[str, Any] | None:
        """One location's detail: snapshot fields + occupants + store products + jobs.

        Contract shape: {location_id, name, location_type, col, row, capacity,
        open_hour, close_hour, open, occupants: [{agent_id, name}],
        products: [{item_id, name, sell_price, buy_price, stock}],
        jobs: [{job_id, name, wage, duration_minutes}]}. Returns None when the
        world or location is missing.
        """
        runtime = self._runtimes.get(world_id)
        if runtime is None:
            return None
        session = self._session_factory()
        try:
            loc = session.get(
                WorldLocation, {"world_id": world_id, "location_id": location_id}
            )
            if loc is None:
                return None
            detail = self._location_snapshot(loc, runtime.clock.world_time).model_dump()

            occupants = session.scalars(
                select(Agent)
                .where(
                    Agent.world_id == world_id,
                    Agent.location_id == location_id,
                )
                .order_by(Agent.name)
            ).all()
            detail["occupants"] = [
                {"agent_id": agent.agent_id, "name": agent.name} for agent in occupants
            ]

            products: list[dict[str, Any]] = []
            store = session.scalars(
                select(Store).where(
                    Store.world_id == world_id, Store.location_id == location_id
                )
            ).first()
            if store is not None:
                item_names = {
                    item.item_id: item.name
                    for item in session.scalars(
                        select(Item).where(Item.world_id == world_id)
                    ).all()
                }
                rows = session.scalars(
                    select(StoreProduct)
                    .where(
                        StoreProduct.world_id == world_id,
                        StoreProduct.store_id == store.store_id,
                    )
                    .order_by(StoreProduct.item_id)
                ).all()
                products = [
                    {
                        "item_id": row.item_id,
                        "name": item_names.get(row.item_id, row.item_id),
                        "sell_price": row.sell_price,
                        "buy_price": row.buy_price,
                        "stock": row.stock,
                    }
                    for row in rows
                ]
            detail["products"] = products

            jobs = session.scalars(
                select(Job)
                .where(Job.world_id == world_id, Job.location_id == location_id)
                .order_by(Job.job_id)
            ).all()
            detail["jobs"] = [
                {
                    "job_id": job.job_id,
                    "name": job.name,
                    "wage": job.wage,
                    "duration_minutes": job.duration_minutes,
                }
                for job in jobs
            ]
            return detail
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Event log query
    # ------------------------------------------------------------------ #

    def events_after(self, world_id: str, after_sequence: int = 0) -> list[WorldEventEnvelope]:
        """Envelopes with sequence > after_sequence, in order (gap recovery)."""
        session = self._session_factory()
        try:
            rows = session.scalars(
                select(WorldEvent)
                .where(
                    WorldEvent.world_id == world_id,
                    WorldEvent.sequence > after_sequence,
                )
                .order_by(WorldEvent.sequence)
            ).all()
            return [
                WorldEventEnvelope(
                    event_id=row.event_id,
                    sequence=row.sequence,
                    world_id=row.world_id,
                    world_time=row.world_time,
                    type=row.type,
                    payload=row.payload or {},
                    trace_id=row.trace_id,
                )
                for row in rows
            ]
        finally:
            session.close()


def is_location_open(
    location_type: str, open_hour: int, close_hour: int, world_time: int
) -> bool:
    """R8: houses and plazas are always open; others honour [open_hour, close_hour)."""
    if location_type in ("house", "plaza"):
        return True
    hour = (world_time % 1440) // 60
    return open_hour <= hour < close_hour


def next_open_time(open_hour: int, close_hour: int, world_time: int) -> int:
    """World_time of the next opening, strictly after ``world_time`` (R8 waits)."""
    if open_hour == 0 and close_hour == 24:
        return world_time
    today_start = world_time - (world_time % 1440)
    candidate = today_start + open_hour * 60
    if candidate <= world_time:
        candidate += 1440
    return candidate


def count_location_occupants(session: Session, world_id: str, location_id: str) -> int:
    """Agents currently inside a location (capacity check, R15)."""
    return int(
        session.scalar(
            select(func.count())
            .select_from(Agent)
            .where(Agent.world_id == world_id, Agent.location_id == location_id)
        )
        or 0
    )
