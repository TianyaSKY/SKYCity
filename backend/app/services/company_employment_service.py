"""Formal company employment: seeding, recruitment, shifts and payroll.

The service is deliberately additive: the existing EconomyService keeps owning
casual jobs, while formal shifts pay from a company account and put products
into company inventory.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config.gameplay import (
    ATTENDANCE_ABSENT_PENALTY,
    ATTENDANCE_LATE_PENALTY,
    SHIFT_EARLY_WINDOW,
    SHIFT_LATE_LIMIT,
    SHIFT_UPCOMING_REMIND,
)
from app.database.models.agents import Agent
from app.database.models.companies import (
    Company,
    CompanyInventory,
    CompanyTransaction,
    EmploymentContract,
    JobApplication,
    JobOpening,
    LeaveRequest,
    Position,
    WorkShift,
)
from app.database.models.items import Item
from app.database.models.jobs import Job
from app.database.models.locations import WorldLocation
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.stores import Store, StoreProduct
from app.database.models.worlds import World
from app.database.unit_of_work import UnitOfWork
from app.services.payroll_service import PayrollService
from app.services.seed_loader import load_companies, load_jobs, load_stores
from app.world_engine.engine import WorldEngine, WorldRuntime

ACTIVE_EMPLOYMENT = ("active", "on_leave")


class CompanyEmploymentError(ValueError):
    """A user-visible company/employment rule rejection."""


class CompanyEmploymentService:
    def __init__(
            self,
            engine: WorldEngine,
            session_factory: sessionmaker,
            world_data_dir: Path,
    ) -> None:
        self.engine = engine
        self._session_factory = session_factory
        self._world_data_dir = world_data_dir
        self.payroll = PayrollService(engine)
        self._uow = UnitOfWork(session_factory)

    def register_runtime(self, runtime: WorldRuntime) -> None:
        """Register durable formal-shift callbacks for one runtime."""
        runtime.scheduler.register("formal_shift_completed", self.handle_shift_completed)
        runtime.scheduler.register("formal_shift_absence_check", self.handle_absence_check)
        runtime.scheduler.register("formal_shift_upcoming", self.handle_shift_upcoming)

    def register_existing_runtimes(self) -> None:
        for world_id in self.engine.runtime_ids():
            runtime = self.engine.get_runtime(world_id)
            if runtime is not None:
                self.register_runtime(runtime)

    def ensure_seeded(self, world_id: str) -> None:
        """Idempotently seed companies, positions and initial openings."""
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            if world is None:
                raise CompanyEmploymentError("世界不存在")
            self._backfill_static_data(session, world_id)
            for company_seed in load_companies(self._world_data_dir):
                company_id = str(company_seed["company_id"])
                company = session.get(Company, {"world_id": world_id, "company_id": company_id})
                if company is None:
                    company = Company(
                        world_id=world_id,
                        company_id=company_id,
                        name=str(company_seed["name"]),
                        company_type=str(company_seed["company_type"]),
                        location_id=str(company_seed["location_id"]),
                        owner_agent_id=company_seed.get("owner_agent_id"),
                        manager_agent_id=company_seed.get("manager_agent_id"),
                        money=int(company_seed.get("initial_money") or 0),
                        status="active",
                        founded_at=world.world_time,
                    )
                    session.add(company)
                    session.add(
                        CompanyTransaction(
                            world_id=world_id,
                            company_id=company_id,
                            type="initial_capital",
                            amount=company.money,
                            balance_after=company.money,
                            reference_type="company",
                            reference_id=company_id,
                            reason="企业初始资金",
                            world_time=world.world_time,
                        )
                    )
                for position_seed in company_seed.get("positions", []):
                    position_id = str(position_seed["position_id"])
                    position = session.get(
                        Position, {"world_id": world_id, "position_id": position_id}
                    )
                    if position is None:
                        position = Position(
                            world_id=world_id,
                            position_id=position_id,
                            company_id=company_id,
                            job_id=str(position_seed["job_id"]),
                            title=str(position_seed["title"]),
                            description=str(position_seed.get("description") or ""),
                            capacity=int(position_seed.get("capacity") or 1),
                            wage_per_shift=int(position_seed["wage_per_shift"]),
                            shift_start_minute=int(position_seed["shift_start_minute"]),
                            shift_end_minute=int(position_seed["shift_end_minute"]),
                            working_days_json=list(position_seed.get("working_days") or range(7)),
                            status="active",
                        )
                        session.add(position)
                        session.flush()
                    opening = session.scalar(
                        select(JobOpening).where(
                            JobOpening.world_id == world_id,
                            JobOpening.position_id == position_id,
                        )
                    )
                    if opening is None:
                        session.add(
                            JobOpening(
                                world_id=world_id,
                                position_id=position_id,
                                company_id=company_id,
                                vacancies=position.capacity,
                                status="open",
                                opened_at=world.world_time,
                            )
                        )
            # R33: bind stores to their owning company from the store seed
            # (idempotent; covers V1 restores and fresh worlds).
            for store_seed in load_stores(self._world_data_dir):
                store_company_id = store_seed.get("company_id")
                if not store_company_id:
                    continue
                store = session.scalar(
                    select(Store).where(
                        Store.world_id == world_id,
                        Store.store_id == str(store_seed["store_id"]),
                    )
                )
                if store is not None and store.company_id != store_company_id:
                    store.company_id = str(store_company_id)
            session.commit()
        finally:
            session.close()

    def _backfill_static_data(self, session: Session, world_id: str) -> None:
        """Idempotently bring a world's static rows in line with world_data.

        M16: backfills WorldLocation / Job rows that world_data gained (the
        bakery, the formal production recipes), upgrades the farm worker
        position + active contracts to the production job, and stops the
        bread shelf from auto-restocking. Every step is existence-gated, so
        repeated calls (fresh world, V1 restore, V2 restore) are no-ops.
        """
        # 1) Locations the map gained since the world was created.
        config_by_id = {loc.location_id: loc for loc in self.engine.world_config.locations}
        for location_id, loc in config_by_id.items():
            if session.get(WorldLocation, {"world_id": world_id, "location_id": location_id}) is None:
                session.add(
                    WorldLocation(
                        world_id=world_id,
                        location_id=location_id,
                        name=loc.name,
                        location_type=loc.location_type,
                        col=loc.col,
                        row=loc.row,
                        capacity=loc.capacity,
                        open_hour=loc.open_hour,
                        close_hour=loc.close_hour,
                    )
                )
        # 2) Jobs the jobs.json gained (recipes are looked up statically at
        # shift time, so the row only carries the legacy seed columns).
        for job_seed in load_jobs(self._world_data_dir):
            job_id = str(job_seed["job_id"])
            if session.get(Job, {"world_id": world_id, "job_id": job_id}) is None:
                session.add(
                    Job(
                        world_id=world_id,
                        job_id=job_id,
                        name=str(job_seed["name"]),
                        location_id=str(job_seed["location_id"]),
                        interactable_id=str(job_seed["interactable_id"]),
                        duration_minutes=int(job_seed["duration_minutes"]),
                        wage=int(job_seed["wage"]),
                        energy_cost_per_hour=int(job_seed["energy_cost_per_hour"]),
                        products_json=list(job_seed["products"]),
                    )
                )
        # 3) Targeted upgrade: the farm worker position now runs the formal
        # production recipe (old job_farm_field stays as a casual job).
        upgraded = session.scalar(
            select(Position).where(
                Position.world_id == world_id,
                Position.position_id == "position_farm_worker",
                Position.job_id == "job_farm_field",
            )
        )
        if upgraded is not None:
            upgraded.job_id = "job_farm_production"
        for contract in session.scalars(
                select(EmploymentContract).where(
                    EmploymentContract.world_id == world_id,
                    EmploymentContract.position_id == "position_farm_worker",
                    EmploymentContract.job_id == "job_farm_field",
                    EmploymentContract.status.in_(ACTIVE_EMPLOYMENT),
                )
        ):
            contract.job_id = "job_farm_production"
        # 4) The village shop's bread shelf is stocked by the bakery now; the
        # seed no longer auto-restocks it (stock levels stay untouched).
        bread = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"},
        )
        if bread is not None and bread.restock_daily != 0:
            bread.restock_daily = 0

    def list_companies(self, world_id: str) -> list[dict[str, Any]]:
        self.ensure_seeded(world_id)
        session = self._session_factory()
        try:
            companies = session.scalars(
                select(Company).where(Company.world_id == world_id).order_by(Company.company_id)
            ).all()
            return [self._company_dict(session, row) for row in companies]
        finally:
            session.close()

    def list_openings(self, world_id: str) -> list[dict[str, Any]]:
        self.ensure_seeded(world_id)
        session = self._session_factory()
        try:
            rows = session.execute(
                select(JobOpening, Position, Company)
                .join(Position,
                      (Position.world_id == JobOpening.world_id) & (Position.position_id == JobOpening.position_id))
                .join(Company,
                      (Company.world_id == JobOpening.world_id) & (Company.company_id == JobOpening.company_id))
                .where(JobOpening.world_id == world_id, JobOpening.status == "open")
                .order_by(Company.company_id, Position.position_id)
            ).all()
            return [
                {
                    "opening_id": opening.opening_id,
                    "company_id": company.company_id,
                    "company_name": company.name,
                    "position_id": position.position_id,
                    "title": position.title,
                    "description": position.description,
                    "location_id": company.location_id,
                    "vacancies": opening.vacancies,
                    "wage_per_shift": position.wage_per_shift,
                    "shift_start_minute": position.shift_start_minute,
                    "shift_end_minute": position.shift_end_minute,
                }
                for opening, position, company in rows
            ]
        finally:
            session.close()

    def get_company(self, world_id: str, company_id: str) -> dict[str, Any]:
        self.ensure_seeded(world_id)
        session = self._session_factory()
        try:
            company = session.get(Company, {"world_id": world_id, "company_id": company_id})
            if company is None:
                raise CompanyEmploymentError("企业不存在")
            return self._company_dict(session, company)
        finally:
            session.close()

    def list_positions(self, world_id: str, company_id: str) -> list[dict[str, Any]]:
        self.ensure_seeded(world_id)
        session = self._session_factory()
        try:
            if session.get(Company, {"world_id": world_id, "company_id": company_id}) is None:
                raise CompanyEmploymentError("企业不存在")
            positions = session.scalars(
                select(Position)
                .where(Position.world_id == world_id, Position.company_id == company_id)
                .order_by(Position.position_id)
            ).all()
            result: list[dict[str, Any]] = []
            for position in positions:
                filled = int(session.scalar(
                    select(func.count()).select_from(EmploymentContract).where(
                        EmploymentContract.world_id == world_id,
                        EmploymentContract.position_id == position.position_id,
                        EmploymentContract.status.in_(ACTIVE_EMPLOYMENT),
                    )
                ) or 0)
                job = session.get(Job, {"world_id": world_id, "job_id": position.job_id})
                result.append({
                    "position_id": position.position_id,
                    "company_id": position.company_id,
                    "job_id": position.job_id,
                    "job_name": job.name if job is not None else position.job_id,
                    "title": position.title,
                    "description": position.description,
                    "capacity": position.capacity,
                    "filled": filled,
                    "vacancies": max(position.capacity - filled, 0),
                    "wage_per_shift": position.wage_per_shift,
                    "shift_start_minute": position.shift_start_minute,
                    "shift_end_minute": position.shift_end_minute,
                    "working_days": position.working_days_json,
                    "status": position.status,
                })
            return result
        finally:
            session.close()

    def list_employees(self, world_id: str, company_id: str) -> list[dict[str, Any]]:
        session = self._session_factory()
        try:
            if session.get(Company, {"world_id": world_id, "company_id": company_id}) is None:
                raise CompanyEmploymentError("企业不存在")
            rows = session.execute(
                select(EmploymentContract, Agent)
                .join(Agent,
                      (Agent.world_id == EmploymentContract.world_id) & (Agent.agent_id == EmploymentContract.agent_id))
                .where(
                    EmploymentContract.world_id == world_id,
                    EmploymentContract.company_id == company_id,
                    EmploymentContract.status.in_(ACTIVE_EMPLOYMENT),
                )
                .order_by(EmploymentContract.started_at)
            ).all()
            return [
                {**self._contract_dict(contract), "agent_name": agent.name}
                for contract, agent in rows
            ]
        finally:
            session.close()

    def list_company_transactions(
            self, world_id: str, company_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        session = self._session_factory()
        try:
            if session.get(Company, {"world_id": world_id, "company_id": company_id}) is None:
                raise CompanyEmploymentError("企业不存在")
            rows = session.scalars(
                select(CompanyTransaction)
                .where(CompanyTransaction.world_id == world_id, CompanyTransaction.company_id == company_id)
                .order_by(CompanyTransaction.world_time.desc())
                .limit(min(max(limit, 1), 200))
            ).all()
            return [self._company_tx_dict(row) for row in rows]
        finally:
            session.close()

    def list_company_inventory(
            self, world_id: str, company_id: str
    ) -> list[dict[str, Any]]:
        """One company's warehouse: totals, reservations and availability."""
        self.ensure_seeded(world_id)
        session = self._session_factory()
        try:
            if session.get(Company, {"world_id": world_id, "company_id": company_id}) is None:
                raise CompanyEmploymentError("企业不存在")
            item_names = {
                item.item_id: item.name
                for item in session.scalars(
                    select(Item).where(Item.world_id == world_id)
                )
            }
            rows = session.scalars(
                select(CompanyInventory)
                .where(
                    CompanyInventory.world_id == world_id,
                    CompanyInventory.company_id == company_id,
                )
                .order_by(CompanyInventory.item_id)
            ).all()
            return [
                {
                    "item_id": row.item_id,
                    "item_name": item_names.get(row.item_id, row.item_id),
                    "quantity": row.quantity,
                    "reserved_quantity": row.reserved_quantity,
                    "available_quantity": row.quantity - row.reserved_quantity,
                }
                for row in rows
            ]
        finally:
            session.close()

    def purchase_company_goods(
            self,
            world_id: str,
            buyer_company_id: str,
            seller_company_id: str,
            manager_agent_id: str,
            item_id: str,
            quantity: int = 1,
            reason: str = "",
            trace_id: str | None = None,
    ) -> dict[str, Any]:
        """R36: one manager-driven fixed-price company-to-company purchase.

        Runs inside a retrying BEGIN IMMEDIATE transaction; the conditional
        UPDATE on the seller's available stock is the race guard (R4-style):
        two managers buying the last units — exactly one succeeds.
        """
        def _inner(session: Session) -> dict[str, Any]:
            world = self._world(session, world_id)
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                raise CompanyEmploymentError("世界运行时不存在")
            if buyer_company_id == seller_company_id:
                raise CompanyEmploymentError("不能向自己采购")
            buyer = session.get(
                Company,
                {"world_id": world_id, "company_id": buyer_company_id},
            )
            seller = session.get(
                Company,
                {"world_id": world_id, "company_id": seller_company_id},
            )
            if buyer is None or seller is None:
                raise CompanyEmploymentError("企业不存在")
            if buyer.status != "active" or seller.status != "active":
                raise CompanyEmploymentError("企业未在经营")
            if buyer.manager_agent_id != manager_agent_id:
                raise CompanyEmploymentError("没有管理该企业采购的权限")
            rule: dict[str, Any] | None = None
            for company_seed in load_companies(self._world_data_dir):
                if company_seed["company_id"] != buyer_company_id:
                    continue
                for entry in company_seed.get("procurement") or []:
                    if (
                        entry.get("item_id") == item_id
                        and entry.get("seller_company_id") == seller_company_id
                    ):
                        rule = entry
                        break
                break
            if rule is None:
                raise CompanyEmploymentError("没有该商品的采购规则")
            unit_price = int(rule.get("unit_price") or 0)
            max_qty = int(rule.get("max_quantity_per_order") or 99)
            if not 1 <= quantity <= max_qty:
                raise CompanyEmploymentError(f"单次采购数量需在 1~{max_qty} 之间")
            total = unit_price * quantity
            if buyer.money < total:
                raise CompanyEmploymentError("企业余额不足")
            result = session.execute(
                update(CompanyInventory)
                .where(
                    CompanyInventory.world_id == world_id,
                    CompanyInventory.company_id == seller_company_id,
                    CompanyInventory.item_id == item_id,
                    CompanyInventory.quantity - CompanyInventory.reserved_quantity >= quantity,
                )
                .values(quantity=CompanyInventory.quantity - quantity)
            )
            if result.rowcount == 0:
                raise CompanyEmploymentError("卖方库存不足")
            buyer.money -= total
            seller.money += total
            self._ensure_inventory(
                session, world_id, buyer_company_id, item_id
            ).quantity += quantity
            item = session.get(Item, {"world_id": world_id, "item_id": item_id})
            item_name = item.name if item is not None else item_id
            session.add(
                CompanyTransaction(
                    world_id=world_id,
                    company_id=buyer_company_id,
                    type="material_purchase",
                    amount=-total,
                    balance_after=buyer.money,
                    related_item_id=item_id,
                    quantity=quantity,
                    reference_type="company",
                    reference_id=seller_company_id,
                    reason=f"采购 {item_name}×{quantity}（{reason}）".strip(),
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            session.add(
                CompanyTransaction(
                    world_id=world_id,
                    company_id=seller_company_id,
                    type="wholesale_sale",
                    amount=total,
                    balance_after=seller.money,
                    related_item_id=item_id,
                    quantity=quantity,
                    reference_type="company",
                    reference_id=buyer_company_id,
                    reason=f"批发 {item_name}×{quantity} 给 {buyer.name}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            self._publish(session, world, "company_money_changed", {
                "company_id": buyer_company_id,
                "amount": -total,
                "balance": buyer.money,
                "reason": f"采购 {item_name}×{quantity}",
            }, trace_id)
            self._publish(session, world, "company_money_changed", {
                "company_id": seller_company_id,
                "amount": total,
                "balance": seller.money,
                "reason": f"批发 {item_name}×{quantity} 给 {buyer.name}",
            }, trace_id)
            self._publish(session, world, "company_inventory_changed", {
                "company_id": buyer_company_id,
                "items": self._company_inventory_list(session, world_id, buyer_company_id),
            }, trace_id)
            self._publish(session, world, "company_inventory_changed", {
                "company_id": seller_company_id,
                "items": self._company_inventory_list(session, world_id, seller_company_id),
            }, trace_id)
            self._publish(session, world, "company_purchase_completed", {
                "company_id": buyer_company_id,
                "seller_company_id": seller_company_id,
                "item_id": item_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "total": total,
            }, trace_id)
            return {
                "company_id": buyer_company_id,
                "seller_company_id": seller_company_id,
                "item_id": item_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "total": total,
                "buyer_balance": buyer.money,
                "seller_balance": seller.money,
            }

        return self._uow.run(_inner)

    def stock_store(
            self,
            world_id: str,
            company_id: str,
            store_id: str,
            manager_agent_id: str,
            item_id: str,
            quantity: int = 1,
            reason: str = "",
            trace_id: str | None = None,
    ) -> dict[str, Any]:
        """R38: move warehouse goods onto the company's own store shelf.

        Same atomicity as purchases: conditional UPDATEs guard both the
        warehouse withdrawal and the shelf capacity (stock + qty <= cap).
        """
        def _inner(session: Session) -> dict[str, Any]:
            world = self._world(session, world_id)
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                raise CompanyEmploymentError("世界运行时不存在")
            if not 1 <= quantity <= 99:
                raise CompanyEmploymentError("单次上架数量需在 1~99 之间")
            company = session.get(
                Company, {"world_id": world_id, "company_id": company_id}
            )
            if company is None:
                raise CompanyEmploymentError("企业不存在")
            if company.manager_agent_id != manager_agent_id:
                raise CompanyEmploymentError("没有管理该企业上架的权限")
            store = session.get(Store, {"world_id": world_id, "store_id": store_id})
            if store is None:
                raise CompanyEmploymentError("商店不存在")
            if store.company_id != company_id:
                raise CompanyEmploymentError("商店不属于该企业")
            result = session.execute(
                update(CompanyInventory)
                .where(
                    CompanyInventory.world_id == world_id,
                    CompanyInventory.company_id == company_id,
                    CompanyInventory.item_id == item_id,
                    CompanyInventory.quantity - CompanyInventory.reserved_quantity >= quantity,
                )
                .values(quantity=CompanyInventory.quantity - quantity)
            )
            if result.rowcount == 0:
                raise CompanyEmploymentError("企业仓库库存不足")
            product = session.get(
                StoreProduct,
                {"world_id": world_id, "store_id": store_id, "item_id": item_id},
            )
            if product is None:
                raise CompanyEmploymentError("商店不售卖该商品")
            shelf = session.execute(
                update(StoreProduct)
                .where(
                    StoreProduct.world_id == world_id,
                    StoreProduct.store_id == store_id,
                    StoreProduct.item_id == item_id,
                    StoreProduct.stock + quantity <= StoreProduct.stock_cap,
                )
                .values(stock=StoreProduct.stock + quantity)
            )
            if shelf.rowcount == 0:
                raise CompanyEmploymentError("货架放不下")
            item = session.get(Item, {"world_id": world_id, "item_id": item_id})
            item_name = item.name if item is not None else item_id
            self._publish(session, world, "company_inventory_changed", {
                "company_id": company_id,
                "items": self._company_inventory_list(session, world_id, company_id),
            }, trace_id)
            self._publish(session, world, "company_store_stocked", {
                "company_id": company_id,
                "store_id": store_id,
                "item_id": item_id,
                "quantity": quantity,
                "stock_after": product.stock,
            }, trace_id)
            return {
                "company_id": company_id,
                "store_id": store_id,
                "item_id": item_id,
                "quantity": quantity,
                "stock_after": product.stock,
            }

        return self._uow.run(_inner)

    def list_agent_shifts(self, world_id: str, agent_id: str) -> list[dict[str, Any]]:
        session = self._session_factory()
        try:
            shifts = session.scalars(
                select(WorkShift)
                .where(WorkShift.world_id == world_id, WorkShift.agent_id == agent_id)
                .order_by(WorkShift.scheduled_start.desc())
                .limit(50)
            ).all()
            return [self._shift_dict(row) for row in shifts]
        finally:
            session.close()

    def apply(self, world_id: str, opening_id: str, agent_id: str, reason: str) -> dict[str, Any]:
        self.ensure_seeded(world_id)
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            opening = session.get(JobOpening, opening_id)
            if opening is None or opening.world_id != world_id or opening.status != "open":
                raise CompanyEmploymentError("招聘不存在或已关闭")
            if opening.vacancies <= 0:
                raise CompanyEmploymentError("岗位已满")
            if session.get(Agent, {"world_id": world_id, "agent_id": agent_id}) is None:
                raise CompanyEmploymentError("智能体不存在")
            application = JobApplication(
                world_id=world_id,
                opening_id=opening_id,
                position_id=opening.position_id,
                company_id=opening.company_id,
                agent_id=agent_id,
                status="submitted",
                applied_at=world.world_time,
                applicant_reason=reason,
            )
            session.add(application)
            self._publish(session, world, "job_application_submitted", {
                "application_id": application.application_id,
                "opening_id": opening_id,
                "company_id": opening.company_id,
                "position_id": opening.position_id,
                "agent_id": agent_id,
                "reason": reason,
            })
            session.commit()
            return self._application_dict(application)
        except IntegrityError as exc:
            session.rollback()
            raise CompanyEmploymentError("已经申请过该职位") from exc
        finally:
            session.close()

    def withdraw(self, world_id: str, application_id: str, agent_id: str) -> dict[str, Any]:
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            application = session.get(JobApplication, application_id)
            if application is None or application.world_id != world_id:
                raise CompanyEmploymentError("申请不存在")
            if application.agent_id != agent_id:
                raise CompanyEmploymentError("不能撤回他人的申请")
            if application.status != "submitted":
                raise CompanyEmploymentError("申请已经处理")
            application.status = "withdrawn"
            self._publish(session, world, "job_application_withdrawn", {
                "application_id": application.application_id,
                "opening_id": application.opening_id,
                "company_id": application.company_id,
                "position_id": application.position_id,
                "agent_id": agent_id,
            })
            session.commit()
            return self._application_dict(application)
        finally:
            session.close()

    def review(
            self,
            world_id: str,
            application_id: str,
            manager_agent_id: str,
            decision: str,
            reason: str,
    ) -> dict[str, Any]:
        if decision not in {"accept", "reject"}:
            raise CompanyEmploymentError("decision 必须是 accept 或 reject")
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            application = session.get(JobApplication, application_id)
            if application is None or application.world_id != world_id:
                raise CompanyEmploymentError("申请不存在")
            if application.status != "submitted":
                raise CompanyEmploymentError("申请已经处理")
            company = session.get(
                Company, {"world_id": world_id, "company_id": application.company_id}
            )
            if company is None or company.manager_agent_id != manager_agent_id:
                raise CompanyEmploymentError("没有审核该企业申请的权限")
            opening = session.get(JobOpening, application.opening_id)
            position = session.get(
                Position, {"world_id": world_id, "position_id": application.position_id}
            )
            if opening is None or position is None:
                raise CompanyEmploymentError("招聘或岗位不存在")
            application.reviewed_at = world.world_time
            application.reviewed_by_agent_id = manager_agent_id
            application.manager_reason = reason
            if decision == "reject":
                application.status = "rejected"
                event_type = "job_application_rejected"
                employment = None
            else:
                active = session.scalar(
                    select(EmploymentContract).where(
                        EmploymentContract.world_id == world_id,
                        EmploymentContract.agent_id == application.agent_id,
                        EmploymentContract.status.in_(ACTIVE_EMPLOYMENT),
                    )
                )
                if active is not None:
                    raise CompanyEmploymentError("申请人已经有正式工作")
                if opening.status != "open" or opening.vacancies <= 0:
                    raise CompanyEmploymentError("岗位已满或招聘已关闭")
                application.status = "accepted"
                opening.vacancies -= 1
                if opening.vacancies == 0:
                    opening.status = "filled"
                employment = EmploymentContract(
                    world_id=world_id,
                    company_id=company.company_id,
                    position_id=position.position_id,
                    job_id=position.job_id,
                    agent_id=application.agent_id,
                    status="active",
                    hired_at=world.world_time,
                    started_at=world.world_time,
                    wage_per_shift=position.wage_per_shift,
                )
                session.add(employment)
                session.flush()
                self._create_next_shift(session, world, employment, position)
                event_type = "employment_started"
            self._publish(session, world, event_type, {
                "application_id": application.application_id,
                "company_id": application.company_id,
                "position_id": application.position_id,
                "agent_id": application.agent_id,
                "manager_agent_id": manager_agent_id,
                "reason": reason,
                "employment_id": employment.employment_id if employment else None,
            })
            session.commit()
            return {
                "application": self._application_dict(application),
                "employment_id": employment.employment_id if employment else None,
            }
        finally:
            session.close()

    def list_agent_employment(self, world_id: str, agent_id: str) -> dict[str, Any]:
        session = self._session_factory()
        try:
            contract = session.scalar(
                select(EmploymentContract).where(
                    EmploymentContract.world_id == world_id,
                    EmploymentContract.agent_id == agent_id,
                    EmploymentContract.status.in_(ACTIVE_EMPLOYMENT),
                )
            )
            if contract is None:
                return {"employment": None, "shifts": []}
            shifts = session.scalars(
                select(WorkShift)
                .where(WorkShift.world_id == world_id, WorkShift.employment_id == contract.employment_id)
                .order_by(WorkShift.scheduled_start.desc())
                .limit(20)
            ).all()
            return {
                "employment": self._contract_dict(contract),
                "shifts": [self._shift_dict(row) for row in shifts],
            }
        finally:
            session.close()

    def start_shift(self, world_id: str, shift_id: str, agent_id: str) -> dict[str, Any]:
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                raise CompanyEmploymentError("世界运行时不存在")
            self.register_runtime(runtime)
            shift = session.get(WorkShift, shift_id)
            if shift is None or shift.world_id != world_id or shift.agent_id != agent_id:
                raise CompanyEmploymentError("班次不存在")
            if shift.status != "scheduled":
                raise CompanyEmploymentError("班次不是待签到状态")
            contract = session.get(EmploymentContract, shift.employment_id)
            position = session.get(Position, {"world_id": world_id, "position_id": shift.position_id})
            company = session.get(Company, {"world_id": world_id, "company_id": shift.company_id})
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if contract is None or contract.status != "active" or position is None or company is None or agent is None:
                raise CompanyEmploymentError("劳动合同或岗位无效")
            if company.status != "active":
                raise CompanyEmploymentError("企业未在经营")
            if agent.action_type is not None:
                raise CompanyEmploymentError("当前行动未完成")
            if agent.location_id != company.location_id:
                raise CompanyEmploymentError("不在工作地点")
            if world.world_time < shift.scheduled_start - SHIFT_EARLY_WINDOW:
                raise CompanyEmploymentError("尚未到签到时间")
            if world.world_time > shift.scheduled_start + SHIFT_LATE_LIMIT:
                raise CompanyEmploymentError("已超过最晚签到时间")
            # M16 R37: reserve the production inputs before the shift starts.
            # Each input is a conditional UPDATE (quantity >= qty); any shortfall
            # aborts the whole transaction — the shift stays scheduled and no
            # completion callback is scheduled. Farm production has no inputs,
            # so this is a no-op for it.
            recipe = next(
                (
                    seed
                    for seed in load_jobs(self._world_data_dir)
                    if seed["job_id"] == contract.job_id
                ),
                None,
            )
            for input_spec in (recipe or {}).get("inputs") or []:
                input_item = str(input_spec.get("item_id") or "")
                input_qty = int(input_spec.get("quantity") or 0)
                if not input_item or input_qty <= 0:
                    continue
                reserved = session.execute(
                    update(CompanyInventory)
                    .where(
                        CompanyInventory.world_id == world_id,
                        CompanyInventory.company_id == company.company_id,
                        CompanyInventory.item_id == input_item,
                        CompanyInventory.quantity >= input_qty,
                    )
                    .values(
                        reserved_quantity=CompanyInventory.reserved_quantity + input_qty
                    )
                )
                if reserved.rowcount == 0:
                    raise CompanyEmploymentError("生产原料不足")
            completion_trace = uuid.uuid4().hex
            shift.actual_start = world.world_time
            shift.late_minutes = max(world.world_time - shift.scheduled_start, 0)
            shift.status = "late" if shift.late_minutes else "in_progress"
            if shift.late_minutes:
                contract.late_shifts += 1
                contract.attendance_score = max(0.0, contract.attendance_score - ATTENDANCE_LATE_PENALTY)
            duration = max(shift.scheduled_end - max(world.world_time, shift.scheduled_start), 1)
            end_at = world.world_time + duration
            agent.action_type = "formal_work"
            agent.action_started_at = world.world_time
            agent.action_ends_at = end_at
            agent.action_data = {"shift_id": shift.shift_id, "company_id": company.company_id}
            runtime.scheduler.schedule(
                session,
                agent_id,
                "formal_shift_completed",
                end_at,
                {"shift_id": shift.shift_id, "trace_id": completion_trace},
            )
            runtime.scheduler.schedule(
                session,
                agent_id,
                "formal_shift_absence_check",
                shift.scheduled_start + 120,
                {"shift_id": shift.shift_id},
            )
            self._publish(session, world, "shift_started", {
                "shift_id": shift.shift_id,
                "employment_id": shift.employment_id,
                "company_id": shift.company_id,
                "agent_id": agent_id,
                "late_minutes": shift.late_minutes,
                "ends_at": end_at,
            })
            # M16 R37: the reservation moved reserved_quantity — sync the
            # warehouse cache in the same transaction (same trace as the
            # eventual completion payload).
            self._publish(session, world, "company_inventory_changed", {
                "company_id": company.company_id,
                "items": self._company_inventory_list(session, world_id, company.company_id),
            }, completion_trace)
            session.commit()
            return self._shift_dict(shift)
        finally:
            session.close()

    def resign(self, world_id: str, employment_id: str, agent_id: str, reason: str) -> dict[str, Any]:
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            contract = session.get(EmploymentContract, employment_id)
            if contract is None or contract.world_id != world_id or contract.agent_id != agent_id:
                raise CompanyEmploymentError("劳动合同不存在")
            if contract.status not in ACTIVE_EMPLOYMENT:
                raise CompanyEmploymentError("劳动合同已经结束")
            contract.status = "resigned"
            contract.ended_at = world.world_time
            contract.termination_reason = reason
            for shift in session.scalars(
                    select(WorkShift).where(
                        WorkShift.world_id == world_id,
                        WorkShift.employment_id == employment_id,
                        WorkShift.status == "scheduled",
                    )
            ):
                shift.status = "cancelled"
            for request in session.scalars(
                    select(LeaveRequest).where(
                        LeaveRequest.world_id == world_id,
                        LeaveRequest.employment_id == employment_id,
                        LeaveRequest.status == "pending",
                    )
            ):
                request.status = "cancelled"
            position = session.get(Position, {"world_id": world_id, "position_id": contract.position_id})
            opening = session.scalar(
                select(JobOpening).where(
                    JobOpening.world_id == world_id,
                    JobOpening.position_id == contract.position_id,
                )
            )
            if opening is None and position is not None:
                opening = JobOpening(
                    world_id=world_id,
                    position_id=position.position_id,
                    company_id=position.company_id,
                    vacancies=1,
                    status="open",
                    opened_at=world.world_time,
                )
                session.add(opening)
            elif opening is not None:
                opening.vacancies += 1
                opening.status = "open"
            self._publish(session, world, "employment_resigned", {
                "employment_id": employment_id,
                "company_id": contract.company_id,
                "agent_id": agent_id,
                "reason": reason,
            })
            session.commit()
            return self._contract_dict(contract)
        finally:
            session.close()

    def terminate(
            self,
            world_id: str,
            employment_id: str,
            manager_agent_id: str,
            reason: str,
    ) -> dict[str, Any]:
        """R32: manager dismisses an employee of their own company."""
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            contract = session.get(EmploymentContract, employment_id)
            if contract is None or contract.world_id != world_id:
                raise CompanyEmploymentError("劳动合同不存在")
            if contract.status not in ACTIVE_EMPLOYMENT:
                raise CompanyEmploymentError("劳动合同已经结束")
            company = session.get(
                Company, {"world_id": world_id, "company_id": contract.company_id}
            )
            if company is None or company.manager_agent_id != manager_agent_id:
                raise CompanyEmploymentError("没有解雇该企业员工的权限")
            contract.status = "terminated"
            contract.ended_at = world.world_time
            contract.termination_reason = reason
            for shift in session.scalars(
                    select(WorkShift).where(
                        WorkShift.world_id == world_id,
                        WorkShift.employment_id == employment_id,
                        WorkShift.status == "scheduled",
                    )
            ):
                shift.status = "cancelled"
            for request in session.scalars(
                    select(LeaveRequest).where(
                        LeaveRequest.world_id == world_id,
                        LeaveRequest.employment_id == employment_id,
                        LeaveRequest.status == "pending",
                    )
            ):
                request.status = "cancelled"
            position = session.get(
                Position, {"world_id": world_id, "position_id": contract.position_id}
            )
            opening = session.scalar(
                select(JobOpening).where(
                    JobOpening.world_id == world_id,
                    JobOpening.position_id == contract.position_id,
                )
            )
            if opening is None and position is not None:
                opening = JobOpening(
                    world_id=world_id,
                    position_id=position.position_id,
                    company_id=position.company_id,
                    vacancies=1,
                    status="open",
                    opened_at=world.world_time,
                )
                session.add(opening)
            elif opening is not None:
                opening.vacancies += 1
                opening.status = "open"
            self._publish(session, world, "employment_terminated", {
                "employment_id": employment_id,
                "company_id": contract.company_id,
                "agent_id": contract.agent_id,
                "manager_agent_id": manager_agent_id,
                "reason": reason,
            })
            session.commit()
            return self._contract_dict(contract)
        finally:
            session.close()

    def pause_recruitment(self, world_id: str, position_id: str, manager_agent_id: str) -> dict[str, Any]:
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            position = session.get(Position, {"world_id": world_id, "position_id": position_id})
            if position is None:
                raise CompanyEmploymentError("岗位不存在")
            company = session.get(
                Company, {"world_id": world_id, "company_id": position.company_id}
            )
            if company is None or company.manager_agent_id != manager_agent_id:
                raise CompanyEmploymentError("没有管理该企业招聘的权限")
            if position.status != "active":
                raise CompanyEmploymentError("岗位当前不是招聘状态")
            position.status = "paused"
            for opening in session.scalars(
                    select(JobOpening).where(
                        JobOpening.world_id == world_id,
                        JobOpening.position_id == position_id,
                        JobOpening.status == "open",
                    )
            ):
                opening.status = "paused"
                self._publish(session, world, "job_opening_closed", {
                    "opening_id": opening.opening_id,
                    "company_id": company.company_id,
                    "position_id": position_id,
                    "reason": "招聘暂停",
                })
            session.commit()
            return {"position_id": position_id, "status": position.status}
        finally:
            session.close()

    def resume_recruitment(self, world_id: str, position_id: str, manager_agent_id: str) -> dict[str, Any]:
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            position = session.get(Position, {"world_id": world_id, "position_id": position_id})
            if position is None:
                raise CompanyEmploymentError("岗位不存在")
            company = session.get(
                Company, {"world_id": world_id, "company_id": position.company_id}
            )
            if company is None or company.manager_agent_id != manager_agent_id:
                raise CompanyEmploymentError("没有管理该企业招聘的权限")
            if position.status != "paused":
                raise CompanyEmploymentError("岗位当前不是暂停状态")
            if company.status != "active":
                raise CompanyEmploymentError("企业未在经营，无法恢复招聘")
            position.status = "active"
            for opening in session.scalars(
                    select(JobOpening).where(
                        JobOpening.world_id == world_id,
                        JobOpening.position_id == position_id,
                    )
            ):
                if opening.vacancies > 0:
                    opening.status = "open"
                    self._publish(session, world, "job_opening_created", {
                        "opening_id": opening.opening_id,
                        "company_id": company.company_id,
                        "position_id": position_id,
                        "vacancies": opening.vacancies,
                    })
            session.commit()
            return {"position_id": position_id, "status": position.status}
        finally:
            session.close()

    def suspend_company(self, world_id: str, company_id: str, manager_agent_id: str, reason: str) -> dict[str, Any]:
        """R32: suspended companies stop hiring and shift generation."""
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            company = session.get(Company, {"world_id": world_id, "company_id": company_id})
            if company is None:
                raise CompanyEmploymentError("企业不存在")
            if company.manager_agent_id != manager_agent_id:
                raise CompanyEmploymentError("没有管理该企业的权限")
            if company.status != "active":
                raise CompanyEmploymentError("企业当前不是经营状态")
            company.status = "suspended"
            company.suspended_at = world.world_time
            for opening in session.scalars(
                    select(JobOpening).where(
                        JobOpening.world_id == world_id,
                        JobOpening.company_id == company_id,
                        JobOpening.status == "open",
                    )
            ):
                opening.status = "paused"
            for shift in session.scalars(
                    select(WorkShift).where(
                        WorkShift.world_id == world_id,
                        WorkShift.company_id == company_id,
                        WorkShift.status == "scheduled",
                    )
            ):
                shift.status = "cancelled"
            self._publish(session, world, "company_status_changed", {
                "company_id": company_id,
                "old_status": "active",
                "new_status": "suspended",
                "reason": reason,
            })
            session.commit()
            return self._company_dict(session, company)
        finally:
            session.close()

    def resume_company(self, world_id: str, company_id: str, manager_agent_id: str, reason: str) -> dict[str, Any]:
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            company = session.get(Company, {"world_id": world_id, "company_id": company_id})
            if company is None:
                raise CompanyEmploymentError("企业不存在")
            if company.manager_agent_id != manager_agent_id:
                raise CompanyEmploymentError("没有管理该企业的权限")
            if company.status != "suspended":
                raise CompanyEmploymentError("企业当前不是暂停状态")
            company.status = "active"
            company.suspended_at = None
            for opening in session.scalars(
                    select(JobOpening).where(
                        JobOpening.world_id == world_id,
                        JobOpening.company_id == company_id,
                    )
            ):
                if opening.vacancies > 0:
                    opening.status = "open"
            # Regenerate the next shift for every active contract whose
            # future shifts were cancelled during the suspension.
            for contract in session.scalars(
                    select(EmploymentContract).where(
                        EmploymentContract.world_id == world_id,
                        EmploymentContract.company_id == company_id,
                        EmploymentContract.status.in_(ACTIVE_EMPLOYMENT),
                    )
            ):
                has_scheduled = session.scalar(
                    select(WorkShift).where(
                        WorkShift.world_id == world_id,
                        WorkShift.employment_id == contract.employment_id,
                        WorkShift.status == "scheduled",
                    )
                )
                if has_scheduled is None:
                    position = session.get(
                        Position,
                        {"world_id": world_id, "position_id": contract.position_id},
                    )
                    if position is not None:
                        self._create_next_shift(session, world, contract, position)
            self._publish(session, world, "company_status_changed", {
                "company_id": company_id,
                "old_status": "suspended",
                "new_status": "active",
                "reason": reason,
            })
            session.commit()
            return self._company_dict(session, company)
        finally:
            session.close()

    def request_leave(self, world_id: str, shift_id: str, agent_id: str, reason: str) -> dict[str, Any]:
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            shift = session.get(WorkShift, shift_id)
            if shift is None or shift.world_id != world_id or shift.agent_id != agent_id:
                raise CompanyEmploymentError("班次不存在")
            if shift.status != "scheduled":
                raise CompanyEmploymentError("班次不是待签到状态")
            contract = session.get(EmploymentContract, shift.employment_id)
            if contract is None or contract.status != "active":
                raise CompanyEmploymentError("劳动合同无效")
            existing = session.scalar(
                select(LeaveRequest).where(
                    LeaveRequest.world_id == world_id,
                    LeaveRequest.shift_id == shift_id,
                    LeaveRequest.status == "pending",
                )
            )
            if existing is not None:
                raise CompanyEmploymentError("该班次已有待审批的请假申请")
            request = LeaveRequest(
                world_id=world_id,
                shift_id=shift_id,
                employment_id=shift.employment_id,
                company_id=shift.company_id,
                agent_id=agent_id,
                status="pending",
                reason=reason,
                requested_at=world.world_time,
            )
            session.add(request)
            self._publish(session, world, "shift_leave_requested", {
                "request_id": request.request_id,
                "shift_id": shift_id,
                "employment_id": shift.employment_id,
                "company_id": shift.company_id,
                "agent_id": agent_id,
                "reason": reason,
            })
            session.commit()
            return self._leave_dict(request)
        finally:
            session.close()

    def review_leave_request(
            self,
            world_id: str,
            request_id: str,
            manager_agent_id: str,
            decision: str,
            reason: str,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise CompanyEmploymentError("decision 必须是 approve 或 reject")
        session = self._session_factory()
        try:
            world = self._world(session, world_id)
            request = session.get(LeaveRequest, request_id)
            if request is None or request.world_id != world_id:
                raise CompanyEmploymentError("请假申请不存在")
            if request.status != "pending":
                raise CompanyEmploymentError("请假申请已经处理")
            company = session.get(
                Company, {"world_id": world_id, "company_id": request.company_id}
            )
            if company is None or company.manager_agent_id != manager_agent_id:
                raise CompanyEmploymentError("没有审批该企业请假的权限")
            shift = session.get(WorkShift, request.shift_id)
            if shift is None or shift.status != "scheduled":
                raise CompanyEmploymentError("班次已不在待签到状态")
            request.reviewed_at = world.world_time
            request.reviewed_by_agent_id = manager_agent_id
            request.manager_reason = reason
            if decision == "approve":
                request.status = "approved"
                shift.status = "leave"
                event_type = "shift_leave_approved"
                # Make the leave status visible, then generate the next free
                # slot (the leave shift never completes or turns absent, so
                # the chain must continue here — R26).
                session.flush()
                contract = session.get(EmploymentContract, request.employment_id)
                if contract is not None and contract.status == "active" and company.status == "active":
                    position = session.get(
                        Position,
                        {"world_id": world_id, "position_id": shift.position_id},
                    )
                    if position is not None:
                        self._create_next_shift(session, world, contract, position)
            else:
                request.status = "rejected"
                event_type = "shift_leave_rejected"
            self._publish(session, world, event_type, {
                "request_id": request.request_id,
                "shift_id": request.shift_id,
                "employment_id": request.employment_id,
                "company_id": request.company_id,
                "agent_id": request.agent_id,
                "manager_agent_id": manager_agent_id,
                "reason": reason,
            })
            session.commit()
            return self._leave_dict(request)
        finally:
            session.close()

    def handle_absence_check(self, session: Session, action: ScheduledAction) -> None:
        shift = session.get(WorkShift, (action.payload or {}).get("shift_id"))
        if shift is None or shift.status != "scheduled":
            return
        world = session.get(World, action.world_id)
        contract = session.get(EmploymentContract, shift.employment_id)
        if world is None or contract is None:
            return
        shift.status = "absent"
        shift.absence_reason = "未在最晚签到时间前到岗"
        contract.absent_shifts += 1
        contract.attendance_score = max(0.0, contract.attendance_score - ATTENDANCE_ABSENT_PENALTY)
        # Pending leave requests for a shift that already turned absent expire.
        for request in session.scalars(
                select(LeaveRequest).where(
                    LeaveRequest.world_id == action.world_id,
                    LeaveRequest.shift_id == shift.shift_id,
                    LeaveRequest.status == "pending",
                )
        ):
            request.status = "expired"
        self._publish(session, world, "shift_absent", {
            "shift_id": shift.shift_id,
            "employment_id": shift.employment_id,
            "company_id": shift.company_id,
            "agent_id": shift.agent_id,
        })
        position = session.get(Position, {"world_id": action.world_id, "position_id": shift.position_id})
        if position is not None and contract.status == "active":
            company = session.get(
                Company,
                {"world_id": action.world_id, "company_id": shift.company_id},
            )
            if company is not None and company.status == "active":
                self._create_next_shift(session, world, contract, position)

    def handle_shift_upcoming(self, session: Session, action: ScheduledAction) -> None:
        """R26: 60 game minutes before the shift, remind + boost the employee."""
        shift = session.get(WorkShift, (action.payload or {}).get("shift_id"))
        if shift is None or shift.status != "scheduled":
            return
        world = session.get(World, action.world_id)
        contract = session.get(EmploymentContract, shift.employment_id)
        if world is None or contract is None or contract.status != "active":
            return
        self._publish(session, world, "shift_upcoming", {
            "shift_id": shift.shift_id,
            "employment_id": shift.employment_id,
            "company_id": shift.company_id,
            "agent_id": shift.agent_id,
            "scheduled_start": shift.scheduled_start,
            "scheduled_end": shift.scheduled_end,
            "minutes_until_start": max(shift.scheduled_start - world.world_time, 0),
        })
        runtime = self.engine.get_runtime(world.world_id)
        if runtime is not None and world.autonomous:
            agent = session.get(
                Agent, {"world_id": world.world_id, "agent_id": shift.agent_id}
            )
            if agent is not None and agent.action_type is None:
                runtime.scheduler.schedule(
                    session,
                    shift.agent_id,
                    "agent_decide",
                    world.world_time + 1,
                    {"origin": "shift_boost"},
                )

    def handle_shift_completed(self, session: Session, action: ScheduledAction) -> None:
        shift = session.get(WorkShift, (action.payload or {}).get("shift_id"))
        agent = session.get(Agent, {"world_id": action.world_id, "agent_id": action.agent_id})
        world = session.get(World, action.world_id)
        if shift is None or world is None or agent is None or shift.status not in {"in_progress", "late"}:
            return
        contract = session.get(EmploymentContract, shift.employment_id)
        company = session.get(Company, {"world_id": action.world_id, "company_id": shift.company_id})
        position = session.get(Position, {"world_id": action.world_id, "position_id": shift.position_id})
        job = session.get(Job, {"world_id": action.world_id, "job_id": contract.job_id}) if contract else None
        if contract is None or company is None or position is None or job is None:
            return
        shift.actual_end = world.world_time
        shift.worked_minutes = max(world.world_time - (shift.actual_start or world.world_time), 0)
        scheduled_minutes = max(shift.scheduled_end - shift.scheduled_start, 1)
        shift.wage_due = contract.wage_per_shift * min(shift.worked_minutes, scheduled_minutes) // scheduled_minutes
        trace_id = str((action.payload or {}).get("trace_id") or uuid.uuid4().hex)
        # M16 R37: settle production against the static recipe. The recipe
        # (inputs + outputs) comes from world_data at runtime; a shift whose
        # job has no recipe (pre-M16 save) falls back to the legacy
        # products_json path — produce only, never consume.
        recipe = next(
            (
                seed
                for seed in load_jobs(self._world_data_dir)
                if seed["job_id"] == contract.job_id
            ),
            None,
        )
        produced: list[dict[str, Any]] = []
        consumed: list[dict[str, Any]] = []
        if recipe is not None:
            for input_spec in recipe.get("inputs") or []:
                input_item = str(input_spec.get("item_id") or "")
                input_qty = int(input_spec.get("quantity") or 0)
                if not input_item or input_qty <= 0:
                    continue
                row = session.get(
                    CompanyInventory,
                    {"world_id": action.world_id, "company_id": company.company_id,
                     "item_id": input_item},
                )
                if row is None:
                    continue
                consume = min(row.reserved_quantity, input_qty)
                if consume > 0:
                    row.quantity -= consume
                    row.reserved_quantity -= consume
                    consumed.append({"item_id": input_item, "quantity": consume})
            for product in recipe.get("products") or []:
                item_id = str(product.get("item_id") or "")
                quantity = int(product.get("quantity") or 0)
                if not item_id or quantity <= 0 or session.get(
                        Item, {"world_id": action.world_id, "item_id": item_id}
                ) is None:
                    continue
                row = session.get(
                    CompanyInventory,
                    {"world_id": action.world_id, "company_id": company.company_id,
                     "item_id": item_id},
                )
                if row is None:
                    row = CompanyInventory(
                        world_id=action.world_id, company_id=company.company_id,
                        item_id=item_id, quantity=0,
                    )
                    session.add(row)
                row.quantity += quantity
                produced.append({"item_id": item_id, "quantity": quantity})
        else:
            for product in job.products_json or []:
                item_id = str(product.get("item_id") or "")
                quantity = int(product.get("quantity") or 0)
                if not item_id or quantity <= 0 or session.get(
                        Item, {"world_id": action.world_id, "item_id": item_id}
                ) is None:
                    continue
                row = session.get(
                    CompanyInventory,
                    {"world_id": action.world_id, "company_id": company.company_id,
                     "item_id": item_id},
                )
                if row is None:
                    row = CompanyInventory(
                        world_id=action.world_id, company_id=company.company_id,
                        item_id=item_id, quantity=0,
                    )
                    session.add(row)
                row.quantity += quantity
                produced.append({"item_id": item_id, "quantity": quantity})
        payroll_event = self.payroll.settle_shift(
            session, world, shift, contract, company, agent,
            shift.wage_due, trace_id,
        )
        shift.status = "completed"
        shift.output_json = produced
        contract.completed_shifts += 1
        agent.action_type = None
        agent.action_started_at = None
        agent.action_ends_at = None
        agent.action_data = None
        self._publish(session, world, "shift_completed", {
            "shift_id": shift.shift_id, "employment_id": contract.employment_id,
            "company_id": company.company_id, "agent_id": agent.agent_id,
            "worked_minutes": shift.worked_minutes, "products": produced,
        }, trace_id)
        self._publish(session, world, payroll_event, {
            "shift_id": shift.shift_id, "employment_id": contract.employment_id,
            "company_id": company.company_id, "agent_id": agent.agent_id,
            "wage_due": shift.wage_due, "wage_paid": shift.wage_paid,
            "company_balance": company.money,
        }, trace_id)
        # M16 R37: warehouse sync + the production ledger event (same txn).
        self._publish(session, world, "company_inventory_changed", {
            "company_id": company.company_id,
            "items": self._company_inventory_list(session, action.world_id, company.company_id),
        }, trace_id)
        self._publish(session, world, "company_production_completed", {
            "company_id": company.company_id,
            "shift_id": shift.shift_id,
            "consumed": consumed,
            "products": produced,
        }, trace_id)
        # R29: whenever the company can afford it, outstanding unpaid wages
        # are repaid in the same transaction (natural payroll retry).
        if contract.unpaid_wage > 0:
            self.payroll.repay_contract(
                session, world, contract, company, agent, trace_id
            )
        if company.status == "active":
            self._create_next_shift(session, world, contract, position)
        if self.engine.action_service is not None:
            self.engine.action_service._maybe_schedule_next_decision(session, action)

    def _create_next_shift(self, session: Session, world: World, contract: EmploymentContract,
                           position: Position) -> WorkShift:
        day = max(world.world_time // 1440, 0)
        for offset in range(0, 8):
            candidate_day = day + offset
            weekday = candidate_day % 7
            start = candidate_day * 1440 + position.shift_start_minute
            if start <= world.world_time or weekday not in position.working_days_json:
                continue
            existing = session.scalar(select(WorkShift).where(
                WorkShift.world_id == world.world_id,
                WorkShift.employment_id == contract.employment_id,
                WorkShift.scheduled_start == start,
            ))
            if existing is not None:
                if existing.status == "scheduled":
                    return existing  # idempotent: already planned
                continue  # slot consumed by leave/absent/completed/cancelled
            shift = WorkShift(
                world_id=world.world_id,
                employment_id=contract.employment_id,
                company_id=contract.company_id,
                position_id=contract.position_id,
                agent_id=contract.agent_id,
                scheduled_start=start,
                scheduled_end=candidate_day * 1440 + position.shift_end_minute,
                status="scheduled",
            )
            session.add(shift)
            session.flush()
            runtime = self.engine.get_runtime(world.world_id)
            if runtime is not None:
                self.register_runtime(runtime)
                runtime.scheduler.schedule(session, contract.agent_id, "formal_shift_absence_check", start + SHIFT_LATE_LIMIT,
                                           {"shift_id": shift.shift_id})
                upcoming_at = start - SHIFT_UPCOMING_REMIND
                if upcoming_at > world.world_time:
                    runtime.scheduler.schedule(
                        session,
                        contract.agent_id,
                        "formal_shift_upcoming",
                        upcoming_at,
                        {"shift_id": shift.shift_id},
                    )
            self._publish(session, world, "shift_scheduled", {
                "shift_id": shift.shift_id, "employment_id": contract.employment_id,
                "company_id": contract.company_id, "agent_id": contract.agent_id,
                "scheduled_start": shift.scheduled_start, "scheduled_end": shift.scheduled_end,
            })
            return shift
        raise CompanyEmploymentError("无法生成下一班次")

    def _company_dict(self, session: Session, company: Company) -> dict[str, Any]:
        employee_count = int(session.scalar(select(func.count()).select_from(EmploymentContract).where(
            EmploymentContract.world_id == company.world_id,
            EmploymentContract.company_id == company.company_id,
            EmploymentContract.status.in_(ACTIVE_EMPLOYMENT),
        )) or 0)
        openings = int(session.scalar(select(func.sum(JobOpening.vacancies)).where(
            JobOpening.world_id == company.world_id,
            JobOpening.company_id == company.company_id,
            JobOpening.status == "open",
        )) or 0)
        return {
            "company_id": company.company_id, "name": company.name,
            "company_type": company.company_type, "location_id": company.location_id,
            "manager_agent_id": company.manager_agent_id, "money": company.money,
            "status": company.status, "employee_count": employee_count,
            "open_vacancies": openings, "unpaid_wage_total": company.unpaid_wage_total,
        }

    @staticmethod
    def _application_dict(row: JobApplication) -> dict[str, Any]:
        return {key: getattr(row, key) for key in (
            "application_id", "world_id", "opening_id", "position_id", "company_id",
            "agent_id", "status", "applied_at", "reviewed_at", "reviewed_by_agent_id",
            "applicant_reason", "manager_reason",
        )}

    @staticmethod
    def _contract_dict(row: EmploymentContract) -> dict[str, Any]:
        return {key: getattr(row, key) for key in (
            "employment_id", "world_id", "company_id", "position_id", "job_id", "agent_id",
            "status", "hired_at", "started_at", "ended_at", "wage_per_shift",
            "attendance_score", "performance_score", "completed_shifts", "late_shifts",
            "absent_shifts", "unpaid_wage", "termination_reason",
        )}

    @staticmethod
    def _shift_dict(row: WorkShift) -> dict[str, Any]:
        return {key: getattr(row, key) for key in (
            "shift_id", "world_id", "employment_id", "company_id", "position_id", "agent_id",
            "scheduled_start", "scheduled_end", "actual_start", "actual_end", "status",
            "late_minutes", "worked_minutes", "wage_due", "wage_paid", "payroll_status",
            "output_json", "absence_reason",
        )}

    @staticmethod
    def _company_inventory_list(
            session: Session, world_id: str, company_id: str
    ) -> list[dict[str, Any]]:
        """Full warehouse list for events (item_id/quantity/reserved_quantity)."""
        rows = session.scalars(
            select(CompanyInventory)
            .where(
                CompanyInventory.world_id == world_id,
                CompanyInventory.company_id == company_id,
            )
            .order_by(CompanyInventory.item_id)
        ).all()
        return [
            {
                "item_id": row.item_id,
                "quantity": row.quantity,
                "reserved_quantity": row.reserved_quantity,
            }
            for row in rows
        ]

    @staticmethod
    def _ensure_inventory(
            session: Session, world_id: str, company_id: str, item_id: str
    ) -> CompanyInventory:
        row = session.get(
            CompanyInventory,
            {"world_id": world_id, "company_id": company_id, "item_id": item_id},
        )
        if row is None:
            row = CompanyInventory(
                world_id=world_id, company_id=company_id, item_id=item_id, quantity=0
            )
            session.add(row)
            session.flush()
        return row

    @staticmethod
    def _company_tx_dict(row: CompanyTransaction) -> dict[str, Any]:
        return {key: getattr(row, key) for key in (
            "transaction_id", "world_id", "company_id", "type", "amount",
            "balance_after", "related_agent_id", "related_item_id", "quantity",
            "reference_type", "reference_id", "reason", "world_time", "trace_id",
        )}

    @staticmethod
    def _leave_dict(row: LeaveRequest) -> dict[str, Any]:
        return {key: getattr(row, key) for key in (
            "request_id", "world_id", "shift_id", "employment_id", "company_id",
            "agent_id", "status", "reason", "manager_reason", "requested_at",
            "reviewed_at", "reviewed_by_agent_id",
        )}

    @staticmethod
    def _world(session: Session, world_id: str) -> World:
        world = session.get(World, world_id)
        if world is None:
            raise CompanyEmploymentError("世界不存在")
        if world.paused:
            raise CompanyEmploymentError("世界已暂停")
        return world

    def _publish(
            self,
            session: Session,
            world: World,
            event_type: str,
            payload: dict[str, Any],
            trace_id: str | None = None,
    ) -> None:
        runtime = self.engine.get_runtime(world.world_id)
        if runtime is not None:
            runtime.event_bus.publish(session, world.world_time, event_type, payload, trace_id)
