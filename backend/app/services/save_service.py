"""SaveService (M9): serialize a world, restore it into a NEW world, replay.

docs/world-rules.md R17: a save captures world time / weather / speed /
autonomous, agent identity + state + action + needs + counters, inventories,
store product stock, jobs, employments, relationships, memories,
conversations + messages, pending scheduled actions (due_at absolute — the
saved world_time keeps them valid), llm_runs and the max event sequence,
plus map_version and schema_version.

Restore builds a brand-new world row (``world_NNN`` via
``engine._next_world_number``) and re-inserts every saved entity under it.
Locations are re-seeded from the world config (map-derived, not saved);
god_actions and world_events stay with the original world (replay reads the
original). The restored world's event bus continues the saved sequence, so
the first event published after restore has sequence ``saved_max + 1``
(R16 continuity across the save boundary).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.conversations import Conversation, ConversationMessage
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.jobs import Employment, Job
from app.database.models.llm_runs import LLMRun
from app.database.models.world_events import WorldEvent
from app.database.models.locations import WorldLocation
from app.database.models.memories import Memory
from app.database.models.relationships import Relationship
from app.database.models.saves import Save
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.stores import Store, StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
from app.world_engine.clock import WorldClock
from app.world_engine.engine import WorldEngine, WorldRuntime

SCHEMA_VERSION = 1

MSG_WORLD_MISSING = "世界不存在"
MSG_SAVE_MISSING = "存档不存在"
MSG_SCHEMA_UNSUPPORTED = "存档版本不支持"

# The restore announcement is a meta event (it fires before the restored
# world's agents witness anything), so memory/relationship derivation is
# suspended for that single publish — otherwise every agent would gain an
# extra "witnessed world restore" episodic memory and the restored memory set
# would drift from the saved one.
RESTORE_ANNOUNCEMENT = "世界已从存档 {save_id} 恢复"


@dataclass(frozen=True)
class SaveServiceResult:
    """Outcome of one save (contract: save_id / world_id / created_at)."""

    save_id: str
    world_id: str
    created_at: int


def _iso(value: Any) -> Any:
    """JSON-friendly: datetimes become ISO strings; everything else passes."""
    return value.isoformat() if isinstance(value, datetime) else value


def _from_iso(value: Any) -> Any:
    """Inverse of ``_iso``: ISO strings parse back to datetimes when possible."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


class SaveService:
    """Owns the saves table for all worlds (one instance, like the engine
    services)."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # Save
    # ------------------------------------------------------------------ #

    def save(self, world_id: str) -> SaveServiceResult:
        """Serialize the world's full state into a new saves row.

        Raises 404 when the world (or its runtime) is missing.
        """
        runtime = self.engine.get_runtime(world_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail=MSG_WORLD_MISSING)
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            if world is None:
                raise HTTPException(status_code=404, detail=MSG_WORLD_MISSING)
            payload = self._serialize(session, world, runtime)
            save_id = f"save_{uuid.uuid4().hex[:12]}"
            session.add(
                Save(
                    save_id=save_id,
                    world_id=world_id,
                    payload_json=payload,
                    map_version=str(payload["map_version"]),
                    created_at=world.world_time,
                )
            )
            session.commit()
            return SaveServiceResult(
                save_id=save_id, world_id=world_id, created_at=world.world_time
            )
        finally:
            session.close()

    def _serialize(
        self, session: Session, world: World, runtime: WorldRuntime
    ) -> dict[str, Any]:
        """One JSON payload per R17 (schema_version 1)."""
        world_id = world.world_id

        def rows(model: Any) -> list[dict[str, Any]]:
            return [
                self._row_dict(row)
                for row in session.scalars(
                    select(model).where(model.world_id == world_id)
                )
            ]

        stores = session.scalars(
            select(Store).where(Store.world_id == world_id).order_by(Store.store_id)
        ).all()
        products_by_store: dict[str, list[dict[str, Any]]] = {}
        for product in session.scalars(
            select(StoreProduct)
            .where(StoreProduct.world_id == world_id)
            .order_by(StoreProduct.store_id, StoreProduct.item_id)
        ):
            products_by_store.setdefault(product.store_id, []).append(
                self._row_dict(product)
            )

        messages = session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.world_id == world_id)
            .order_by(ConversationMessage.conversation_id, ConversationMessage.message_id)
        ).all()

        return {
            "schema_version": SCHEMA_VERSION,
            "map_version": self.engine.world_config.map_version,
            # Highest allocated event sequence of the ORIGINAL world; the
            # restored world continues from here (first new event = max + 1).
            "max_sequence": runtime.event_bus.sequence,
            "world": {
                "name": world.name,
                "world_time": world.world_time,
                "speed": world.speed,
                "paused": world.paused,
                "weather": world.weather,
                "autonomous": world.autonomous,
            },
            "agents": rows(Agent),
            "items": rows(Item),
            "stores": [
                {
                    "store_id": store.store_id,
                    "location_id": store.location_id,
                    "products": products_by_store.get(store.store_id, []),
                }
                for store in stores
            ],
            "jobs": rows(Job),
            "employments": rows(Employment),
            "inventories": rows(Inventory),
            "relationships": rows(Relationship),
            "memories": rows(Memory),
            "conversations": rows(Conversation),
            "conversation_messages": [self._row_dict(row) for row in messages],
            "transactions": rows(Transaction),
            "scheduled_actions": rows(ScheduledAction),
            "llm_runs": rows(LLMRun),
            # Full event log: the restored world re-points it, so its replay
            # shows the complete history (origin + continuation) contiguously.
            "events": rows(WorldEvent),
        }

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        """Every mapped column as a JSON-safe dict (datetimes -> ISO)."""
        return {
            column.key: _iso(getattr(row, column.key))
            for column in inspect(row).mapper.column_attrs
        }

    # ------------------------------------------------------------------ #
    # Restore
    # ------------------------------------------------------------------ #

    def restore(self, save_id: str) -> WorldRuntime:
        """Rebuild a NEW world from the save and start it running.

        Returns the new world's runtime. Raises 404 for a missing save, 400
        for an unsupported payload schema.
        """
        session = self._session_factory()
        try:
            save = session.get(Save, save_id)
            if save is None:
                raise HTTPException(status_code=404, detail=MSG_SAVE_MISSING)
            payload = save.payload_json or {}
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise HTTPException(
                    status_code=400,
                    detail=f"{MSG_SCHEMA_UNSUPPORTED}: {payload.get('schema_version')}",
                )
            world_data = payload.get("world") or {}
            world_time = int(world_data.get("world_time") or 480)
            speed = int(world_data.get("speed") or 1)

            number = self.engine._next_world_number(session)
            world_id = f"world_{number:03d}"
            world = World(
                world_id=world_id,
                name=str(world_data.get("name") or f"世界 {number:03d}"),
                world_time=world_time,
                speed=speed,
                paused=False,  # a restored world starts running; user can pause
                weather=str(world_data.get("weather") or "clear"),
                autonomous=bool(world_data.get("autonomous") or False),
            )
            session.add(world)
            self._reinsert(session, payload, world_id)
            # Make every inserted row visible to the scheduler queries below
            # (SessionLocal has autoflush=False).
            session.flush()

            runtime = self.engine._ensure_runtime(
                world_id,
                clock=WorldClock(world_time, speed, paused=False),
                session=session,
            )
            # R16: the new world's sequence continues the saved one.
            runtime.event_bus.restore_sequence(int(payload.get("max_sequence") or 0))

            # Announce the restoration; suspended derivation keeps the
            # restored memory/relationship sets identical to the save.
            hook = runtime.event_bus.on_publish
            runtime.event_bus.on_publish = None
            try:
                runtime.event_bus.publish(
                    session,
                    world_time,
                    "world_event_created",
                    {"text": RESTORE_ANNOUNCEMENT.format(save_id=save_id), "importance": "normal"},
                )
            finally:
                runtime.event_bus.on_publish = hook

            if world.autonomous:
                self.engine._schedule_initial_decisions(
                    session, runtime, world, base_delay=2
                )
                self.engine.memory_service.ensure_daily_reflection_scheduled(
                    session, runtime, world.world_time
                )
            session.commit()
        finally:
            session.close()
        return runtime

    def _reinsert(
        self, session: Session, payload: dict[str, Any], world_id: str
    ) -> None:
        """Insert every saved entity under ``world_id`` (FK-safe order)."""
        # Locations are map-derived (R17: map only stores its version), so
        # they are re-seeded from the current world config instead of saved.
        for loc in self.engine.world_config.locations:
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

        for row in payload.get("agents", []):
            data = self._row_data(row)
            # An in-flight LLM decision cannot be resumed; clear the guard so
            # the restored world's decision loop is never wedged.
            data["is_deciding"] = False
            session.add(Agent(world_id=world_id, **data))

        for row in payload.get("items", []):
            session.add(Item(world_id=world_id, **self._row_data(row)))

        for store in payload.get("stores", []):
            session.add(
                Store(
                    world_id=world_id,
                    store_id=store["store_id"],
                    location_id=store["location_id"],
                )
            )
            for product in store.get("products", []):
                data = self._row_data(product)
                data["store_id"] = store["store_id"]
                session.add(StoreProduct(world_id=world_id, **data))

        for row in payload.get("jobs", []):
            session.add(Job(world_id=world_id, **self._row_data(row)))
        for row in payload.get("employments", []):
            session.add(Employment(world_id=world_id, **self._row_data(row)))
        for row in payload.get("inventories", []):
            session.add(Inventory(world_id=world_id, **self._row_data(row)))
        for row in payload.get("relationships", []):
            session.add(Relationship(world_id=world_id, **self._row_data(row)))
        for row in payload.get("memories", []):
            data = self._fresh_pk(Memory, self._row_data(row))
            session.add(Memory(world_id=world_id, **data))
        # Conversations/messages have GLOBAL primary keys: regenerate both and
        # remap messages to their new conversation.
        conversation_ids: dict[str, str] = {}
        for row in payload.get("conversations", []):
            data = self._fresh_pk(Conversation, self._row_data(row))
            conversation_ids[row["conversation_id"]] = data["conversation_id"]
            session.add(Conversation(world_id=world_id, **data))
        for row in payload.get("conversation_messages", []):
            data = self._fresh_pk(ConversationMessage, self._row_data(row))
            data["conversation_id"] = conversation_ids.get(
                data["conversation_id"], data["conversation_id"]
            )
            session.add(ConversationMessage(world_id=world_id, **data))
        for row in payload.get("transactions", []):
            data = self._fresh_pk(Transaction, self._row_data(row))
            session.add(Transaction(world_id=world_id, **data))
        # llm_runs re-pointed at the new world; run_id is a GLOBAL primary key
        # so fresh ids are required (the original rows still exist).
        for row in payload.get("llm_runs", []):
            data = self._fresh_pk(LLMRun, self._row_data(row))
            session.add(LLMRun(world_id=world_id, **data))
        # Pending scheduled actions restore with fresh action_ids (global PK);
        # due_at stays absolute because world_time was saved with the payload.
        for row in payload.get("scheduled_actions", []):
            data = self._fresh_pk(ScheduledAction, self._row_data(row))
            session.add(ScheduledAction(world_id=world_id, **data))
        # Full event history re-pointed under the new world (PK is
        # world_id+event_id, so the originals are untouched): the restored
        # world's replay shows the complete chain contiguously.
        for row in payload.get("events", []):
            data = self._row_data(row)
            session.add(WorldEvent(world_id=world_id, **data))

    @staticmethod
    def _fresh_pk(model: Any, data: dict[str, Any]) -> dict[str, Any]:
        """Regenerate the primary key for tables whose PK is global (not
        per-world), so re-pointing rows into the new world cannot collide
        with the originals still stored under the old world."""
        if model is LLMRun:
            data["run_id"] = f"run_{uuid.uuid4().hex}"
        elif model is ScheduledAction:
            data["action_id"] = f"act_{uuid.uuid4().hex}"
        elif model is Transaction:
            data["tx_id"] = f"tx_{uuid.uuid4().hex}"
        elif model is Memory:
            data["memory_id"] = f"mem_{uuid.uuid4().hex[:16]}"
        elif model is Conversation:
            data["conversation_id"] = f"conv_{uuid.uuid4().hex[:16]}"
        elif model is ConversationMessage:
            data["message_id"] = f"msg_{uuid.uuid4().hex[:16]}"
        return data

    @staticmethod
    def _row_data(row: dict[str, Any]) -> dict[str, Any]:
        """One saved row dict -> ORM kwargs (world_id injected by caller)."""
        return {
            key: _from_iso(value)
            for key, value in row.items()
            if key != "world_id"
        }

    # ------------------------------------------------------------------ #
    # Replay + listings
    # ------------------------------------------------------------------ #

    def replay(self, world_id: str) -> dict[str, Any]:
        """Initial snapshot + every event envelope in sequence order.

        Reads the ORIGINAL world: the event stream belongs to it, and a
        restored world is a separate world_id that continues the sequence.
        """
        if self.engine.get_runtime(world_id) is None:
            raise HTTPException(status_code=404, detail=MSG_WORLD_MISSING)
        initial_snapshot = self.engine.snapshot(world_id)
        if initial_snapshot is None:
            raise HTTPException(status_code=404, detail=MSG_WORLD_MISSING)
        envelopes = self.engine.events_after(world_id, 0)
        return {
            "world_id": world_id,
            "initial_snapshot": initial_snapshot,
            "events": [envelope.model_dump() for envelope in envelopes],
        }

    def list_saves(self, world_id: str | None = None) -> list[dict[str, Any]]:
        """All saves (optionally for one world), newest first."""
        session = self._session_factory()
        try:
            stmt = select(Save)
            if world_id is not None:
                stmt = stmt.where(Save.world_id == world_id)
            rows = session.scalars(
                stmt.order_by(Save.created_at.desc(), Save.save_id.desc())
            ).all()
            return [
                {
                    "save_id": row.save_id,
                    "world_id": row.world_id,
                    "created_at": row.created_at,
                }
                for row in rows
            ]
        finally:
            session.close()
