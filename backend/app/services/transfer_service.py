"""TransferService: the M11 rule gate for agent-to-agent transfers & gifts.

World rules enforced here (docs/world-rules.md R19): R19.1 both are instant
actions requiring the initiator to be idle (R1); the target must be within
TALK_DISTANCE (R9, manhattan) but need not be idle — receiving money/items is
passive; R19.2 no credit / no over-giving (R7): insufficient balance/items are
rejected and the conditional UPDATE is the concurrent guard (no self
transfers); R19.3 both sides get a ``transfer`` / ``item_gift`` transaction
row and the balance/inventory deltas arrive via the usual ``money_changed`` /
``inventory_changed`` events.

Both operations run inside the same retrying BEGIN IMMEDIATE transaction as
the economy/stock services. Every accepted action publishes its event
envelopes and returns ``(ok, envelope, reason)`` — the action-service shape.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
from app.database.unit_of_work import UnitOfWork
from app.services.conversation_service import (
    MSG_NOT_NEAR,
    MSG_TARGET_MISSING,
    TALK_DISTANCE,
    manhattan_distance,
)
from app.services.economy_service import (
    MSG_AGENT_MISSING,
    MSG_BUSY,
    MSG_NO_MONEY,
    MSG_NOT_IN_INVENTORY,
    MSG_PAUSED,
    MSG_WORLD_MISSING,
)
from app.world_engine.engine import WorldEngine

# Rejection reasons (Chinese, surfaced in tool results / HTTP 409).
MSG_SELF_TRANSFER = "不能转给自己"

# R19: defensive caps (schema le mirrors these); the real ceiling is the
# sender's balance / holding quantity.
MAX_TRANSFER_AMOUNT = 1_000_000
MAX_GIFT_QUANTITY = 99


class TransferService:
    """Owns the transfer/give rule gate for all worlds (one instance, like
    the EconomyService)."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory
        self._uow = UnitOfWork(session_factory)

    # ------------------------------------------------------------------ #
    # Money transfer (R19.1 instant, R19.2 no credit)
    # ------------------------------------------------------------------ #

    def transfer_money(
        self,
        world_id: str,
        from_agent_id: str,
        to_agent_id: str,
        amount: int = 1,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Transfer ``amount`` coins from one agent to another (R7: no credit)."""

        def _inner(session: Session) -> tuple[bool, Any, str | None]:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            sender = session.get(Agent, {"world_id": world_id, "agent_id": from_agent_id})
            if sender is None:
                return False, None, MSG_AGENT_MISSING
            if sender.action_type is not None:
                return False, None, MSG_BUSY  # R1: the initiator must be idle
            if to_agent_id == from_agent_id:
                return False, None, MSG_SELF_TRANSFER
            target = session.get(Agent, {"world_id": world_id, "agent_id": to_agent_id})
            if target is None:
                return False, None, MSG_TARGET_MISSING
            if (
                manhattan_distance(sender.col, sender.row, target.col, target.row)
                > TALK_DISTANCE
            ):
                return False, None, MSG_NOT_NEAR  # R19.1: same as talk (R9)

            transfer_amount = max(1, min(int(amount), MAX_TRANSFER_AMOUNT))
            result = session.execute(
                update(Agent)
                .where(
                    Agent.world_id == world_id,
                    Agent.agent_id == from_agent_id,
                    Agent.money >= transfer_amount,
                )
                .values(money=Agent.money - transfer_amount)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:
                return False, None, MSG_NO_MONEY  # R7: no credit / lost a race

            sender.money -= transfer_amount  # keep the in-memory agent consistent
            target.money += transfer_amount
            session.add(
                Transaction(
                    world_id=world_id,
                    agent_id=from_agent_id,
                    type="transfer",
                    amount=-transfer_amount,
                    balance_after=sender.money,
                    item_id=None,
                    quantity=None,
                    reason=f"转账给 {target.name}: {reason or ''}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            session.add(
                Transaction(
                    world_id=world_id,
                    agent_id=to_agent_id,
                    type="transfer",
                    amount=transfer_amount,
                    balance_after=target.money,
                    item_id=None,
                    quantity=None,
                    reason=f"收到 {sender.name} 转账: {reason or ''}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "money_transferred",
                {
                    "from_agent_id": from_agent_id,
                    "to_agent_id": to_agent_id,
                    "amount": transfer_amount,
                    "reason": reason,
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "money_changed",
                {
                    "agent_id": from_agent_id,
                    "amount": -transfer_amount,
                    "balance": sender.money,
                    "reason": f"转账给 {target.name}",
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "money_changed",
                {
                    "agent_id": to_agent_id,
                    "amount": transfer_amount,
                    "balance": target.money,
                    "reason": f"收到 {sender.name} 转账",
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Item gift (R19.1 instant, R19.2 no over-giving)
    # ------------------------------------------------------------------ #

    def give_item(
        self,
        world_id: str,
        from_agent_id: str,
        to_agent_id: str,
        item_id: str,
        quantity: int = 1,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Give ``quantity`` of ``item_id`` to another agent (no over-giving)."""

        def _inner(session: Session) -> tuple[bool, Any, str | None]:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            sender = session.get(Agent, {"world_id": world_id, "agent_id": from_agent_id})
            if sender is None:
                return False, None, MSG_AGENT_MISSING
            if sender.action_type is not None:
                return False, None, MSG_BUSY  # R1
            if to_agent_id == from_agent_id:
                return False, None, MSG_SELF_TRANSFER
            target = session.get(Agent, {"world_id": world_id, "agent_id": to_agent_id})
            if target is None:
                return False, None, MSG_TARGET_MISSING
            if (
                manhattan_distance(sender.col, sender.row, target.col, target.row)
                > TALK_DISTANCE
            ):
                return False, None, MSG_NOT_NEAR  # R19.1: same as talk (R9)

            gift_quantity = max(1, min(int(quantity), MAX_GIFT_QUANTITY))
            result = session.execute(
                update(Inventory)
                .where(
                    Inventory.world_id == world_id,
                    Inventory.agent_id == from_agent_id,
                    Inventory.item_id == item_id,
                    Inventory.quantity >= gift_quantity,
                )
                .values(quantity=Inventory.quantity - gift_quantity)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:
                return False, None, MSG_NOT_IN_INVENTORY  # R19.2: no over-giving

            inventory = session.get(
                Inventory,
                {"world_id": world_id, "agent_id": from_agent_id, "item_id": item_id},
            )
            if inventory is not None:
                session.refresh(inventory)  # see the post-UPDATE quantity
            if inventory is not None and inventory.quantity <= 0:
                session.delete(inventory)
            self._add_inventory(session, world_id, to_agent_id, item_id, gift_quantity)
            item = session.get(Item, {"world_id": world_id, "item_id": item_id})
            item_name = item.name if item is not None else item_id
            session.add(
                Transaction(
                    world_id=world_id,
                    agent_id=from_agent_id,
                    type="item_gift",
                    amount=0,
                    balance_after=sender.money,
                    item_id=item_id,
                    quantity=gift_quantity,
                    reason=f"赠予 {target.name} {item_name}×{gift_quantity}: {reason or ''}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            session.add(
                Transaction(
                    world_id=world_id,
                    agent_id=to_agent_id,
                    type="item_gift",
                    amount=0,
                    balance_after=target.money,
                    item_id=item_id,
                    quantity=gift_quantity,
                    reason=f"收到 {sender.name} 赠送 {item_name}×{gift_quantity}: {reason or ''}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "item_given",
                {
                    "from_agent_id": from_agent_id,
                    "to_agent_id": to_agent_id,
                    "item_id": item_id,
                    "item_name": item_name,
                    "quantity": gift_quantity,
                    "reason": reason,
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "inventory_changed",
                {
                    "agent_id": from_agent_id,
                    "items": self._inventory_list(session, world_id, from_agent_id),
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "inventory_changed",
                {
                    "agent_id": to_agent_id,
                    "items": self._inventory_list(session, world_id, to_agent_id),
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Helpers (local copies of the economy service's private helpers — the
    # same precedent as stocks.py copying _clamp/_result_json)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _add_inventory(
        session: Session, world_id: str, agent_id: str, item_id: str, quantity: int
    ) -> None:
        row = session.get(
            Inventory, {"world_id": world_id, "agent_id": agent_id, "item_id": item_id}
        )
        if row is None:
            session.add(
                Inventory(
                    world_id=world_id,
                    agent_id=agent_id,
                    item_id=item_id,
                    quantity=quantity,
                )
            )
        else:
            row.quantity += quantity

    @staticmethod
    def _inventory_list(
        session: Session, world_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        # The caller may have added/deleted inventory rows not yet flushed
        # (autoflush is off); flush so the snapshot list is complete.
        session.flush()
        rows = session.scalars(
            select(Inventory)
            .where(Inventory.world_id == world_id, Inventory.agent_id == agent_id)
            .order_by(Inventory.item_id)
        ).all()
        return [{"item_id": row.item_id, "quantity": row.quantity} for row in rows]
