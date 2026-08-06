"""Company, recruitment, formal employment and payroll persistence models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Company(Base):
    __tablename__ = "companies"

    world_id: Mapped[str] = mapped_column(String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    company_type: Mapped[str] = mapped_column(String(32), nullable=False)
    location_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manager_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    money: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    founded_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unpaid_wage_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        ForeignKeyConstraint(["world_id", "company_id"], ["companies.world_id", "companies.company_id"], ondelete="CASCADE"),
    )

    world_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    wage_per_shift: Mapped[int] = mapped_column(Integer, nullable=False)
    shift_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    shift_end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    working_days_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=lambda: [0, 1, 2, 3, 4])
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")


class JobOpening(Base):
    __tablename__ = "job_openings"
    __table_args__ = (
        ForeignKeyConstraint(["world_id", "position_id"], ["positions.world_id", "positions.position_id"], ondelete="CASCADE"),
    )

    opening_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: _id("opening"))
    world_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False)
    vacancies: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    opened_at: Mapped[int] = mapped_column(Integer, nullable=False)
    closes_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class JobApplication(Base):
    __tablename__ = "job_applications"
    __table_args__ = (
        UniqueConstraint("world_id", "opening_id", "agent_id", name="uq_job_application_opening_agent"),
        Index("ix_job_applications_world_company_status", "world_id", "company_id", "status"),
    )

    application_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: _id("application"))
    world_id: Mapped[str] = mapped_column(String(64), nullable=False)
    opening_id: Mapped[str] = mapped_column(String(48), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="submitted")
    applied_at: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_by_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    applicant_reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    manager_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)


class EmploymentContract(Base):
    __tablename__ = "employment_contracts"
    __table_args__ = (
        Index("ix_employment_contracts_world_agent_status", "world_id", "agent_id", "status"),
    )

    employment_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: _id("employment"))
    world_id: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    hired_at: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False)
    ended_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wage_per_shift: Mapped[int] = mapped_column(Integer, nullable=False)
    attendance_score: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    performance_score: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    completed_shifts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    late_shifts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    absent_shifts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unpaid_wage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    termination_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)


class WorkShift(Base):
    __tablename__ = "work_shifts"
    __table_args__ = (
        UniqueConstraint("world_id", "employment_id", "scheduled_start", name="uq_work_shift_contract_start"),
        Index("ix_work_shifts_world_agent_status", "world_id", "agent_id", "status"),
    )

    shift_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: _id("shift"))
    world_id: Mapped[str] = mapped_column(String(64), nullable=False)
    employment_id: Mapped[str] = mapped_column(String(48), nullable=False)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduled_start: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_end: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="scheduled")
    late_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worked_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wage_due: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wage_paid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payroll_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_due")
    output_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    absence_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)


class CompanyInventory(Base):
    __tablename__ = "company_inventories"

    world_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CompanyTransaction(Base):
    __tablename__ = "company_transactions"
    __table_args__ = (Index("ix_company_transactions_world_company", "world_id", "company_id"),)

    transaction_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: _id("ctx"))
    world_id: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    related_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reference_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    world_time: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
