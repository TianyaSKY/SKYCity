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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from starlette.websockets import WebSocket

from app.config.gameplay import (
    DEBT_MOOD_PENALTY_PER_DAY,
    ENERGY_DRAIN_PER_HOUR,
    HOTEL_LOCATION_ID,
    HOTEL_NIGHTLY_FEE,
    HUNGER_FORCED_EAT_THRESHOLD,
    INITIAL_ENERGY,
    INITIAL_LONELINESS,
    INITIAL_MONEY,
    INITIAL_MOOD,
    INITIAL_SATIETY,
    LONELINESS_BOOST_THRESHOLD,
    LONELINESS_GAIN_PER_HOUR,
    MANAGER_PROFIT_SHARE_PERCENT,
    MOOD_BOOST_THRESHOLD,
    MOOD_DRAIN_PER_HOUR,
    NEEDS_MAX,
    NIGHT_END_HOUR,
    NIGHT_SLEEP_ENERGY_THRESHOLD,
    NIGHT_START_HOUR,
    PROMO_DISCOUNT_PERCENT,
    PROMO_ROLL_DENOMINATOR,
    PROMO_ROLL_HITS,
    SATIETY_DRAIN_PER_HOUR,
    SATIETY_EMPTY_EXTRA_ENERGY_DRAIN,
    SLEEP_ENERGY_PER_HOUR,
    SLEEP_MOOD_PER_HOUR,
    TICK_INTERVAL,
    TREASURY_UBI_SHARE_PERCENT,
    UPKEEP_PER_DAY,
    WAIT_ENERGY_PER_HOUR,
    WAIT_MOOD_PER_HOUR,
    ZOMBIE_LOSS_DAYS,
)
from app.database.models.agents import Agent
from app.database.models.companies import (
    Company,
    CompanyTransaction,
    EmploymentContract,
    JobOpening,
    WorkShift,
)
from app.database.models.crops import Crop
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.jobs import Job
from app.database.models.locations import WorldLocation
from app.database.models.stores import Store, StoreProduct
from app.database.models.structures import TileStructure
from app.database.models.transactions import Transaction
from app.database.models.world_events import WorldEvent
from app.database.models.worlds import World
from app.domain.agent import AgentActionBuild, AgentActionMove, AgentActionWait, AgentActionWork, AgentSnapshot
from app.domain.event import WorldEventEnvelope
from app.domain.location import LocationSnapshot
from app.domain.world import (
    CropSnapshot,
    StoreProductSnapshot,
    StoreSnapshot,
    StructureSnapshot,
    WorldSnapshot,
    WorldSnapshotPayload,
)
from app.services.memory_service import MemoryRecorder, MemoryService
from app.services.relationship_service import RelationshipService
from app.services.seed_loader import (
    BlueprintDef,
    CropDef,
    load_blueprints,
    load_crop_config,
    load_crops,
    load_items,
    load_jobs,
    load_stores,
)
from app.services.world_config_loader import ParsedWorldConfig
from app.world_engine.clock import WorldClock
from app.world_engine.event_bus import EventBus
from app.world_engine.scheduler import Scheduler

def _promo_roll(world_id: str, store_id: str, item_id: str, day: int) -> bool:
    """M12 D5: deterministic promo-day roll for one product (~20%).

    Hash-based (not random) so ``advance_minutes`` fast-forwards in tests
    reproduce exactly the same promo set as a live run. The modulo formula
    stays fixed (PROMO_ROLL_DENOMINATOR/HITS from the global config) so
    existing deterministic outcomes are preserved.
    """
    digest = hashlib.md5(f"{world_id}:{store_id}:{item_id}:{day}".encode()).hexdigest()
    return int(digest[:8], 16) % PROMO_ROLL_DENOMINATOR < PROMO_ROLL_HITS


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
        # agent_id -> home location id, built lazily from the character cards
        # (single source of truth, see home_location_id()).
        self._home_by_agent: dict[str, str] | None = None
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
        # BuildService (M14) is wired after construction; the build tool and
        # the "build_completed" scheduler handler reach it here.
        self.build_service: Any = None
        # CropService (M15) is wired after construction; the plant/harvest
        # tools and the "crop_grow" scheduler handler reach it here.
        self.crop_service: Any = None
        # ShopService (M18) is wired after construction; the open_shop/
        # stock_shop/adjust_price/close_shop tools reach it here.
        self.shop_service: Any = None
        # M14: static blueprint recipes (R22) — loaded once from world_data.
        self.blueprints: dict[str, BlueprintDef] = {
            blueprint.blueprint_id: blueprint
            for blueprint in load_blueprints(world_data_dir)
        }
        # M15: static crop recipes (R23) — seed item -> growth stages + yield.
        self.crops: dict[str, CropDef] = {
            crop.seed_item_id: crop for crop in load_crops(world_data_dir)
        }
        # R23.2: plantable cells = walkable, non-reserved cells within the
        # plant_radius of the farm_field interactable (computed once).
        self.plantable_cells: frozenset[tuple[int, int]] = frozenset(
            self._compute_plantable_cells(world_data_dir)
        )
        # M6: memory + relationship services are self-contained (engine +
        # session factory), so they are constructed here and active in every
        # engine — memory recording and relationship deltas are automatic.
        self.memory_service = MemoryService(self, session_factory)
        self.memory_recorder = MemoryRecorder(self, session_factory, self.memory_service)
        self.relationship_service = RelationshipService(self, session_factory)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def _compute_plantable_cells(
            self, world_data_dir: Path | None
    ) -> set[tuple[int, int]]:
        """R23.2: walkable cells within plant_radius of farm_field, excluding
        location anchors and spawn points."""
        config = load_crop_config(world_data_dir)
        radius = int(config.get("plant_radius") or 4)
        field_id = str(config.get("farm_field_id") or "farm_field")
        field = next(
            (i for i in self.world_config.interactables if i.object_id == field_id),
            None,
        )
        if field is None:
            return set()
        reserved = {
                       (loc.col, loc.row) for loc in self.world_config.locations
                   } | {(sp.col, sp.row) for sp in self.world_config.spawn_points}
        return {
            (col, row)
            for col, row in self.world_config.walkable_cells
            if abs(col - field.col) + abs(row - field.row) <= radius
               and (col, row) not in reserved
        }

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
            # A fresh process has no in-flight decision tasks, so a stale
            # is_deciding=True left by a crashed/restarted process is dead
            # state. Keeping it would wedge the decision loop permanently:
            # decide() claims via ``is_deciding=False -> True`` and would
            # refuse forever, leaving the agent idle for hours.
            session.execute(update(Agent).values(is_deciding=False))
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

    def effective_walkable(
            self, session: Session, world_id: str
    ) -> frozenset[tuple[int, int]]:
        """R22.6/R24: static walkable cells minus blocking built structures,
        plus cells paved by paving blueprints.

        The single source for pathfinding and build connectivity checks —
        a placed structure is a real obstacle, a laid road a real shortcut.
        """
        blocked: set[tuple[int, int]] = set()
        paved: set[tuple[int, int]] = set()
        rows = session.scalars(
            select(TileStructure).where(
                TileStructure.world_id == world_id,
                TileStructure.status == "built",
            )
        ).all()
        for row in rows:
            blueprint = self.blueprints.get(row.blueprint_id)
            if blueprint is None:
                continue
            if blueprint.blocking:
                blocked.add((row.col, row.row))
            elif blueprint.paving:
                paved.add((row.col, row.row))
        return frozenset((self.world_config.walkable_cells - blocked) | paved)

    def home_location_id(self, agent_id: str) -> str | None:
        """The agent's home location id from its character card.

        ``None`` means the agent has no home (sleeping requires the hotel).
        The card is the single source of truth (world_config.spawn_points);
        a home id whose location is missing from the map counts as no home.
        """
        if self._home_by_agent is None:
            valid = {loc.location_id for loc in self.world_config.locations}
            self._home_by_agent = {
                spawn.agent_id: spawn.home_id
                for spawn in self.world_config.spawn_points
                if spawn.home_id and spawn.home_id in valid
            }
        return self._home_by_agent.get(agent_id)

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
        if self.build_service is not None:
            scheduler.register("build_completed", self.build_service.handle_build_completed)
        if self.crop_service is not None:
            scheduler.register("crop_grow", self.crop_service.handle_crop_grow)
        if self.decision_service is not None:
            scheduler.register("agent_decide", self.decision_service.handle_agent_decide)
            # E-full: an action queued behind a conversation lock executes
            # through the same tool dispatch as a normal decision.
            scheduler.register("queued_action", self.decision_service.handle_queued_action)
        if self.conversation_service is not None:
            # E-full: the conversation lock's hard cap ends silent chats so
            # neither member stays locked forever.
            scheduler.register("talk_expired", self.conversation_service.handle_talk_expired)
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
        elif envelope.type == "job_application_submitted":
            # M13 R25: a pending application wakes the company manager so the
            # hiring decision does not wait for the next periodic decision.
            company = session.get(
                Company,
                {"world_id": envelope.world_id, "company_id": payload.get("company_id")},
            )
            if company is not None and company.manager_agent_id:
                manager = session.get(
                    Agent,
                    {"world_id": envelope.world_id, "agent_id": company.manager_agent_id},
                )
                if manager is not None and manager.action_type is None:
                    runtime.scheduler.schedule(
                        session,
                        company.manager_agent_id,
                        "agent_decide",
                        envelope.world_time + 1,
                        {"origin": "application_boost"},
                    )
        elif envelope.type == "shift_leave_requested":
            # M13 R27: pending leave requests also wake the manager.
            company = session.get(
                Company,
                {"world_id": envelope.world_id, "company_id": payload.get("company_id")},
            )
            if company is not None and company.manager_agent_id:
                manager = session.get(
                    Agent,
                    {"world_id": envelope.world_id, "agent_id": company.manager_agent_id},
                )
                if manager is not None and manager.action_type is None:
                    runtime.scheduler.schedule(
                        session,
                        company.manager_agent_id,
                        "agent_decide",
                        envelope.world_time + 1,
                        {"origin": "leave_boost"},
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
        home_id = spawn.home_id
        home_exists = home_id is not None and any(
            loc.location_id == home_id for loc in self.world_config.locations
        )
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
            energy=INITIAL_ENERGY,
            mood=INITIAL_MOOD,
            loneliness=INITIAL_LONELINESS,
            money=int(identity.get("initial_money") or INITIAL_MONEY),
            action_type=None,
            action_started_at=None,
            action_ends_at=None,
            action_data=None,
        )

    def _seed_economy(self, session: Session, world_id: str) -> None:
        """M5: seed per-world items, stores (+ products at full stock) and jobs.

        WorkHistory rows are deliberately absent until the first completed work.
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
                    work_bonus_jobs=seed["work_bonus_jobs"],
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
                    company_id=store_seed.get("company_id"),
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
                        stock=product.get("initial_stock", product["stock_cap"]),  # R15
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
            self._force_hunger_eat(session, runtime, world, world_time)
            self._maybe_restock(session, runtime, world, world_time)
            self._maybe_fulfill_orders(session, runtime, world, world_time)
            self._tick_stock_prices(session, runtime, world, world_time)
            # M8: daily counters reset at the day boundary (midnight crossing).
            day = world_time // 1440
            if runtime.last_day is not None and day != runtime.last_day:
                # M10: dividends settle before the daily counters reset (same
                # transaction, same commit). M17: the manager's daily profit
                # share comes after dividends, before upkeep. M12 D6: upkeep
                # is deducted between dividends and the counter reset.
                self._pay_dividends(session, runtime, world, world_time)
                self._pay_manager_profits(session, runtime, world, world_time)
                self._apply_daily_upkeep(session, runtime, world, world_time)
                self._disburse_treasury(session, runtime, world, world_time)
                self._liquidate_zombie_companies(session, runtime, world, world_time)
                self._reset_daily_counters(session, world.world_id)
        runtime.last_hour = hour
        runtime.last_day = world_time // 1440

    def _maybe_fulfill_orders(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            world_time: int,
    ) -> None:
        """C1: fill pending procurement orders at the hourly tick."""
        service = getattr(self, "company_employment_service", None)
        if service is None:
            return
        try:
            service.fulfill_open_orders(session, world, world_time)
        except Exception:  # noqa: BLE001 - one bad order must not kill the tick
            logger.exception("Order fulfillment failed world={}", world.world_id)

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

    def _pay_manager_profits(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            world_time: int,
    ) -> None:
        """M17: at 00:00 pay each company manager a share of the day's net
        profit (MANAGER_PROFIT_SHARE_PERCENT, floor-divided).

        Net profit = today's CompanyTransaction amounts (initial capital and
        the manager-share rows excluded). No payout when the day lost money,
        the company's treasury cannot cover the share, or the company has no
        manager. Same transaction as dividends/upkeep (one commit).
        """
        # world_time is the first minute of the NEW day; the profit window is
        # the day that just ended: [(world_time - 1) // 1440 * 1440, world_time).
        day_start = ((world_time - 1) // 1440) * 1440
        companies = session.scalars(
            select(Company).where(Company.world_id == world.world_id)
        ).all()
        for company in companies:
            if not company.manager_agent_id:
                continue
            rows = session.scalars(
                select(CompanyTransaction).where(
                    CompanyTransaction.world_id == world.world_id,
                    CompanyTransaction.company_id == company.company_id,
                    CompanyTransaction.world_time >= day_start,
                    CompanyTransaction.world_time < world_time,
                )
            ).all()
            profit = sum(
                row.amount
                for row in rows
                if row.type not in ("initial_capital", "manager_profit")
            )
            if profit <= 0:
                continue
            share = profit * MANAGER_PROFIT_SHARE_PERCENT // 100
            if share <= 0 or company.money < share:
                continue
            manager = session.get(
                Agent,
                {"world_id": world.world_id, "agent_id": company.manager_agent_id},
            )
            if manager is None:
                continue
            company.money -= share
            session.add(
                CompanyTransaction(
                    world_id=world.world_id,
                    company_id=company.company_id,
                    type="manager_profit",
                    amount=-share,
                    balance_after=company.money,
                    related_agent_id=manager.agent_id,
                    reason=f"{company.name} 经理利润分成",
                    world_time=world_time,
                    trace_id="",
                )
            )
            manager.money += share
            session.add(
                Transaction(
                    world_id=world.world_id,
                    agent_id=manager.agent_id,
                    type="manager_profit",
                    amount=share,
                    balance_after=manager.money,
                    reason=f"{company.name} 经理利润分成",
                    world_time=world_time,
                    trace_id="",
                )
            )
            runtime.event_bus.publish(
                session,
                world_time,
                "manager_profit_paid",
                {
                    "company_id": company.company_id,
                    "company_name": company.name,
                    "manager_agent_id": manager.agent_id,
                    "amount": share,
                    "profit": profit,
                },
            )
            runtime.event_bus.publish(
                session,
                world_time,
                "money_changed",
                {
                    "agent_id": manager.agent_id,
                    "amount": share,
                    "balance": manager.money,
                    "reason": f"{company.name} 经理利润分成",
                },
            )

    def _apply_daily_upkeep(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            world_time: int,
    ) -> None:
        """M12 D6: daily cost of living at 00:00.

        Every agent pays the full UPKEEP_PER_DAY out of money. A balance
        shortfall is allowed to go negative — that shortfall is the agent's
        debt, and the daily upkeep is the *only* source of debt: voluntary
        purchases still reject on insufficient balance (R7). While in debt
        (money < 0) the agent also takes a daily mood penalty
        (DEBT_MOOD_PENALTY_PER_DAY, floor 0 — the anxiety of owing money).
        Recorded as an ``upkeep`` transaction plus a ``money_changed`` event;
        the mood penalty, when it moves mood, publishes ``needs_changed``.
        """
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world.world_id)
        ).all()
        for agent in agents:
            agent.money -= UPKEEP_PER_DAY
            # A1: the upkeep is collected into the village treasury instead of
            # being destroyed; _disburse_treasury recycles it the same morning.
            world.treasury += UPKEEP_PER_DAY
            session.add(
                Transaction(
                    world_id=world.world_id,
                    agent_id=agent.agent_id,
                    type="upkeep",
                    amount=-UPKEEP_PER_DAY,
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
                    "amount": -UPKEEP_PER_DAY,
                    "balance": agent.money,
                    "reason": "每日生活开销",
                },
            )
            if agent.money < 0:
                before_mood = agent.mood
                agent.mood = max(0, agent.mood - DEBT_MOOD_PENALTY_PER_DAY)
                if agent.mood != before_mood:
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

    def _reset_daily_counters(self, session: Session, world_id: str) -> None:
        """M8: zero every agent's daily LLM call/token counters (day change)."""
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world_id)
        ).all()
        for agent in agents:
            agent.daily_token_usage = 0
            agent.daily_call_count = 0

    def _liquidate_zombie_companies(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            world_time: int,
    ) -> None:
        """E2: close zombie companies at 00:00.

        A company that has lost money for ZOMBIE_LOSS_DAYS consecutive days
        while owing unpaid wages is liquidated: status -> closed, contracts
        terminated, openings closed, future shifts cancelled. This releases
        the trapped workers and capital instead of letting the dead company
        occupy jobs and openings forever.
        """
        companies = session.scalars(
            select(Company).where(
                Company.world_id == world.world_id,
                Company.status == "active",
            )
        ).all()
        for company in companies:
            if company.consecutive_loss_days < ZOMBIE_LOSS_DAYS:
                continue
            if company.unpaid_wage_total <= 0:
                continue
            company.status = "closed"
            company.closed_at = world_time
            contracts = session.scalars(
                select(EmploymentContract).where(
                    EmploymentContract.world_id == world.world_id,
                    EmploymentContract.company_id == company.company_id,
                    EmploymentContract.status.in_(("active", "on_leave")),
                )
            ).all()
            for contract in contracts:
                contract.status = "terminated"
                contract.ended_at = world_time
                contract.termination_reason = "企业连续亏损，破产清算"
                session.execute(
                    update(WorkShift)
                    .where(
                        WorkShift.world_id == world.world_id,
                        WorkShift.employment_id == contract.employment_id,
                        WorkShift.status == "scheduled",
                    )
                    .values(status="cancelled", absence_reason="企业清算，班次取消")
                )
            session.execute(
                update(JobOpening)
                .where(
                    JobOpening.world_id == world.world_id,
                    JobOpening.company_id == company.company_id,
                    JobOpening.status == "open",
                )
                .values(status="closed")
            )
            runtime.event_bus.publish(
                session,
                world_time,
                "company_status_changed",
                {
                    "company_id": company.company_id,
                    "company_name": company.name,
                    "old_status": "active",
                    "new_status": "closed",
                    "reason": "连续亏损，破产清算",
                },
            )

    def _disburse_treasury(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            world_time: int,
    ) -> None:
        """A1: recycle the village treasury at 00:00.

        The treasury holds the day's upkeep (collected in
        ``_apply_daily_upkeep``). TREASURY_UBI_SHARE_PERCENT goes to every
        resident equally — a universal basic income. Debtors receive it too:
        withholding welfare from the poorest created a poverty trap (no money
        -> cannot buy food -> cannot work -> deeper debt). The debt pressure
        still exists via the daily mood penalty and the ``origin=debt``
        decision boost; the UBI just keeps everyone alive and able to work.
        The remainder is paid to active companies proportional to the wages
        they paid that day (a wage subsidy that directly covers the
        company-side wage gap). Money is recycled, never destroyed.
        """
        if world.treasury <= 0:
            return
        day_start = ((world_time - 1) // 1440) * 1440
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world.world_id)
        ).all()
        ubi_total = world.treasury * TREASURY_UBI_SHARE_PERCENT // 100
        company_pool = world.treasury - ubi_total
        # UBI: equal split among ALL residents (debtors included).
        if ubi_total > 0 and agents:
            per_agent = ubi_total // len(agents)
            if per_agent > 0:
                for agent in agents:
                    agent.money += per_agent
                    world.treasury -= per_agent
                    session.add(
                        Transaction(
                            world_id=world.world_id,
                            agent_id=agent.agent_id,
                            type="ubi_income",
                            amount=per_agent,
                            balance_after=agent.money,
                            item_id=None,
                            quantity=None,
                            reason="村庄基本收入",
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
                            "amount": per_agent,
                            "balance": agent.money,
                            "reason": "村庄基本收入",
                        },
                    )
        # Wage subsidy: proportional to each active company's wage payments
        # in the day that just ended (initial capital and manager shares
        # excluded). Companies that paid no wages get nothing.
        if company_pool <= 0:
            world.treasury = max(world.treasury, 0)
            return
        rows = session.scalars(
            select(CompanyTransaction).where(
                CompanyTransaction.world_id == world.world_id,
                CompanyTransaction.type == "wage_payment",
                CompanyTransaction.world_time >= day_start,
                CompanyTransaction.world_time < world_time,
            )
        ).all()
        by_company: dict[str, int] = {}
        for row in rows:
            by_company[row.company_id] = by_company.get(row.company_id, 0) + abs(row.amount)
        total_wages = sum(by_company.values())
        if total_wages <= 0:
            # No wages paid anywhere — the company pool stays in the treasury
            # as a buffer for future days (money parked, never destroyed).
            world.treasury = max(world.treasury, 0)
            return
        for company in session.scalars(
                select(Company).where(
                    Company.world_id == world.world_id,
                    Company.status == "active",
                )
        ).all():
            wages = by_company.get(company.company_id, 0)
            if wages <= 0:
                continue
            share = company_pool * wages // total_wages
            if share <= 0:
                continue
            company.money += share
            world.treasury -= share
            session.add(
                CompanyTransaction(
                    world_id=world.world_id,
                    company_id=company.company_id,
                    type="treasury_subsidy",
                    amount=share,
                    balance_after=company.money,
                    reference_type="treasury",
                    reference_id=world.world_id,
                    reason="村庄金库工资补贴",
                    world_time=world_time,
                    trace_id="",
                )
            )
            runtime.event_bus.publish(
                session,
                world_time,
                "company_money_changed",
                {
                    "company_id": company.company_id,
                    "amount": share,
                    "balance": company.money,
                    "reason": "村庄金库工资补贴",
                },
            )
        world.treasury = max(world.treasury, 0)

    def _apply_hourly_needs(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            world_time: int,
    ) -> None:
        """R14/M12/R21 hourly needs: drains/gains and per-action recovery,
        all from the global gameplay config (app.config.gameplay)."""
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world.world_id)
        ).all()
        hour = (world_time % 1440) // 60
        night = hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR
        for agent in agents:
            before = (agent.satiety, agent.energy, agent.mood, agent.loneliness)
            agent.satiety = max(0, agent.satiety - SATIETY_DRAIN_PER_HOUR)
            agent.energy = max(0, agent.energy - ENERGY_DRAIN_PER_HOUR)
            agent.mood = max(0, agent.mood - MOOD_DRAIN_PER_HOUR)
            agent.loneliness = min(NEEDS_MAX, agent.loneliness + LONELINESS_GAIN_PER_HOUR)
            if agent.action_type == "wait":
                agent.energy = min(NEEDS_MAX, agent.energy + WAIT_ENERGY_PER_HOUR)
                agent.mood = min(NEEDS_MAX, agent.mood + WAIT_MOOD_PER_HOUR)
            elif agent.action_type == "sleep":
                agent.energy = min(NEEDS_MAX, agent.energy + SLEEP_ENERGY_PER_HOUR)
                agent.mood = min(NEEDS_MAX, agent.mood + SLEEP_MOOD_PER_HOUR)
            if agent.satiety <= 0:
                agent.energy = max(0, agent.energy - SATIETY_EMPTY_EXTRA_ENERGY_DRAIN)
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
            # R11/R12/M12/R21: satiety empty, energy drained, mood low,
            # loneliness high or in debt (money < 0 — owed from daily upkeep)
            # -> high-priority decision. Night + low energy -> go home/hotel
            # to sleep (R14 sleep steering). A debt boost gets its own origin
            # so the reason is visible in the scheduler payload.
            if (
                    world.autonomous
                    and agent.action_type is None
                    and (
                    agent.satiety <= 0
                    or agent.energy <= 0
                    or agent.mood <= MOOD_BOOST_THRESHOLD
                    or agent.loneliness >= LONELINESS_BOOST_THRESHOLD
                    or agent.money < 0
                    or (night and agent.energy <= NIGHT_SLEEP_ENERGY_THRESHOLD)
            )
            ):
                runtime.scheduler.schedule(
                    session,
                    agent.agent_id,
                    "agent_decide",
                    world_time + 1,
                    {"origin": "debt" if agent.money < 0 else "needs_boost"},
                )

    def _force_hunger_eat(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            world_time: int,
    ) -> None:
        """B1: the engine itself guarantees food consumption.

        An idle agent whose satiety has dropped to HUNGER_FORCED_EAT_THRESHOLD
        eats from its own inventory, or buys the cheapest food at the store it
        is standing in (if it can afford it). This runs at the hourly tick,
        independent of the LLM — the demand circuit stays closed even when the
        model ignores its own hunger. An agent that has neither food nor a
        shop nearby falls through to the LLM (whose observation now carries an
        explicit hunger hint).
        """
        if not world.autonomous or self.economy_service is None:
            return
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world.world_id)
        ).all()
        for agent in agents:
            # B1: eat when idle or merely waiting (a wait can be interrupted
            # safely; moving/working/sleeping agents finish what they started).
            if agent.action_type not in (None, "wait"):
                continue
            if agent.satiety > HUNGER_FORCED_EAT_THRESHOLD:
                continue
            self._feed_agent(session, runtime, world, agent, world_time)

    def _feed_agent(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            agent: Agent,
            world_time: int,
    ) -> None:
        """One forced-feeding attempt: inventory food first, then a shop buy.

        Mutates rows inside the engine's own transaction (NOT via
        economy_service, which opens a separate session and would lose the
        uncommitted hourly satiety drain — the classic lost-update).
        """
        # 1) Eat food already in the backpack (first edible item).
        for inv in session.scalars(
                select(Inventory).where(
                    Inventory.world_id == world.world_id,
                    Inventory.agent_id == agent.agent_id,
                    Inventory.quantity > 0,
                )
        ).all():
            item = session.get(
                Item, {"world_id": world.world_id, "item_id": inv.item_id}
            )
            if item is None or item.satiety_restore <= 0:
                continue
            satiety_before = agent.satiety
            agent.satiety = min(NEEDS_MAX, agent.satiety + item.satiety_restore)
            agent.mood = min(NEEDS_MAX, agent.mood + item.mood_restore)
            inv.quantity -= 1
            if inv.quantity <= 0:
                session.delete(inv)
            runtime.event_bus.publish(
                session,
                world_time,
                "item_used",
                {
                    "agent_id": agent.agent_id,
                    "item_id": item.item_id,
                    "item_name": item.name,
                    "satiety_before": satiety_before,
                    "satiety_after": agent.satiety,
                    "mood_before": agent.mood - item.mood_restore,
                    "mood_after": agent.mood,
                },
            )
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
            runtime.event_bus.publish(
                session,
                world_time,
                "inventory_changed",
                {
                    "agent_id": agent.agent_id,
                    "items": self.economy_service._inventory_list(
                        session, world.world_id, agent.agent_id
                    ),
                },
            )
            runtime.event_bus.publish(
                session,
                world_time,
                "world_event_created",
                {
                    "agent_id": agent.agent_id,
                    "text": f"{agent.name} 饿得受不了，吃掉了背包里的食物",
                    "importance": "normal",
                },
            )
            return
        # 2) Buy the cheapest food at the store covering the agent.
        if agent.location_id is None:
            return
        stores = session.scalars(
            select(Store).where(
                Store.world_id == world.world_id,
                Store.location_id == agent.location_id,
            )
        ).all()
        candidates: list[tuple[int, str, str]] = []  # (sell_price, item_id, store_id)
        for store in stores:
            if not store.company_id and not store.owner_agent_id:
                continue  # unbound store would destroy money
            for product in session.scalars(
                    select(StoreProduct).where(
                        StoreProduct.world_id == world.world_id,
                        StoreProduct.store_id == store.store_id,
                        StoreProduct.stock > 0,
                    )
            ).all():
                item = session.get(
                    Item, {"world_id": world.world_id, "item_id": product.item_id}
                )
                if item is None or item.satiety_restore <= 0:
                    continue
                candidates.append((product.sell_price, product.item_id, store.store_id))
        if not candidates:
            return
        candidates.sort()
        price, item_id, store_id = candidates[0]
        if agent.money < price:
            return
        store = session.get(
            Store, {"world_id": world.world_id, "store_id": store_id}
        )
        product = session.get(
            StoreProduct,
            {"world_id": world.world_id, "store_id": store_id, "item_id": item_id},
        )
        if store is None or product is None:
            return
        result = session.execute(
            update(StoreProduct)
            .where(
                StoreProduct.world_id == world.world_id,
                StoreProduct.store_id == store_id,
                StoreProduct.item_id == item_id,
                StoreProduct.stock >= 1,
            )
            .values(stock=StoreProduct.stock - 1)
        )
        if result.rowcount == 0:
            return
        item = session.get(Item, {"world_id": world.world_id, "item_id": item_id})
        item_name = item.name if item is not None else item_id
        agent.money -= price
        self.economy_service._add_inventory(
            session, world.world_id, agent.agent_id, item_id, 1
        )
        company = (
            session.get(
                Company,
                {"world_id": world.world_id, "company_id": store.company_id},
            )
            if store.company_id
            else None
        )
        if company is not None:
            company.money += price
            session.add(
                CompanyTransaction(
                    world_id=world.world_id,
                    company_id=company.company_id,
                    type="sale_income",
                    amount=price,
                    balance_after=company.money,
                    related_agent_id=agent.agent_id,
                    related_item_id=item_id,
                    quantity=1,
                    reference_type="store",
                    reference_id=store_id,
                    reason=f"商店售出 {item_name}×1",
                    world_time=world_time,
                    trace_id="",
                )
            )
        session.add(
            Transaction(
                world_id=world.world_id,
                agent_id=agent.agent_id,
                type="expense",
                amount=-price,
                balance_after=agent.money,
                item_id=item_id,
                quantity=1,
                reason=f"购买 {item_name}×1",
                world_time=world_time,
                trace_id="",
            )
        )
        runtime.event_bus.publish(
            session,
            world_time,
            "item_purchased",
            {
                "agent_id": agent.agent_id,
                "item_id": item_id,
                "item_name": item_name,
                "quantity": 1,
                "unit_price": price,
                "total": price,
                "store_id": store_id,
                "balance": agent.money,
            },
        )
        runtime.event_bus.publish(
            session,
            world_time,
            "money_changed",
            {
                "agent_id": agent.agent_id,
                "amount": -price,
                "balance": agent.money,
                "reason": f"饥饿，购买 {item_name}",
            },
        )
        runtime.event_bus.publish(
            session,
            world_time,
            "inventory_changed",
            {
                "agent_id": agent.agent_id,
                "items": self.economy_service._inventory_list(
                    session, world.world_id, agent.agent_id
                ),
            },
        )
        runtime.event_bus.publish(
            session,
            world_time,
            "world_event_created",
            {
                "agent_id": agent.agent_id,
                "text": f"{agent.name} 饿得受不了，在商店买了食物",
                "importance": "normal",
            },
        )

    def _maybe_restock(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            world_time: int,
    ) -> None:
        # R40: personal shops (owner_agent_id set) have no magic restock and
        # no promo rolls — their shelf only changes via the owner's tools.
        stores = session.scalars(
            select(Store)
            .join(
                WorldLocation,
                (WorldLocation.world_id == Store.world_id)
                & (WorldLocation.location_id == Store.location_id),
            )
            .where(
                Store.world_id == world.world_id,
                Store.owner_agent_id.is_(None),
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
                    max(1, round(product.base_sell_price * (100 - PROMO_DISCOUNT_PERCENT) / 100))
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
            structures = session.scalars(
                select(TileStructure)
                .where(TileStructure.world_id == world_id)
                .order_by(TileStructure.col, TileStructure.row)
            ).all()
            crops = session.scalars(
                select(Crop)
                .where(Crop.world_id == world_id)
                .order_by(Crop.col, Crop.row)
            ).all()
            stores = session.scalars(
                select(Store)
                .where(Store.world_id == world_id)
                .order_by(Store.store_id)
            ).all()
            products_by_store: dict[str, list[Any]] = {}
            for product in session.scalars(
                    select(StoreProduct)
                    .where(StoreProduct.world_id == world_id)
                    .order_by(StoreProduct.store_id, StoreProduct.item_id)
            ):
                products_by_store.setdefault(product.store_id, []).append(product)
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
                structures=[
                    StructureSnapshot(
                        col=row.col,
                        row=row.row,
                        blueprint_id=row.blueprint_id,
                        owner_agent_id=row.owner_agent_id,
                        status=row.status,
                        built_at=row.built_at,
                    )
                    for row in structures
                ],
                crops=[
                    CropSnapshot(
                        col=row.col,
                        row=row.row,
                        item_id=row.item_id,
                        planted_by=row.planted_by,
                        planted_at=row.planted_at,
                        stage=row.stage,
                        next_stage_at=row.next_stage_at,
                    )
                    for row in crops
                ],
                stores=[
                    StoreSnapshot(
                        store_id=row.store_id,
                        name=row.name,
                        location_id=row.location_id,
                        owner_agent_id=row.owner_agent_id,
                        company_id=row.company_id,
                        products=[
                            StoreProductSnapshot(
                                item_id=product.item_id,
                                sell_price=product.sell_price,
                                buy_price=product.buy_price,
                                stock=product.stock,
                                stock_cap=product.stock_cap,
                            )
                            for product in products_by_store.get(row.store_id, [])
                        ],
                    )
                    for row in stores
                ],
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
                path=data.get("path"),
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
        elif agent.action_type == "build":
            data = agent.action_data or {}
            action = AgentActionBuild(
                type="build",
                blueprint_id=str(data.get("blueprint_id") or ""),
                col=int(data.get("col") or agent.col),
                row=int(data.get("row") or agent.row),
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
            # Identity comes from the character card (single source of truth,
            # may carry persona fields beyond the DB columns); the DB row is
            # the fallback for anything the card lacks.
            card = self._load_identity(agent.agent_id)
            detail["identity"] = {
                "id": agent.agent_id,
                "name": card.get("name") or agent.name,
                "age": card.get("age", agent.age),
                "occupation": card.get("occupation") or agent.occupation,
                "background": card.get("background") or agent.background,
                "life_story": card.get("life_story") or "",
                "character_traits": card.get("character_traits") or "",
                "likes": card.get("likes") or [],
                "dislikes": card.get("dislikes") or [],
                "quirks": card.get("quirks") or [],
                "daily_routine": card.get("daily_routine") or "",
                "values": card.get("values") or agent.values or [],
                "long_term_goals": card.get("long_term_goals") or agent.long_term_goals or [],
                "speaking_style": card.get("speaking_style") or agent.speaking_style,
                "personality": card.get("personality") or agent.personality or {},
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
    """R8: houses, hotels and plazas are always open; others honour [open_hour, close_hour)."""
    if location_type in ("house", "hotel", "plaza"):
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
