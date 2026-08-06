"""Company and formal-employment MVP lifecycle tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.companies import Company, EmploymentContract, JobOpening, WorkShift
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.company_employment_service import CompanyEmploymentService
from app.services.economy_service import EconomyService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_world_engine import advance_minutes


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


@pytest.fixture()
def system(world_config: ParsedWorldConfig):
    engine = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    engine.action_service = ActionExecutionService(engine, SessionLocal)
    engine.economy_service = EconomyService(engine, SessionLocal)
    service = CompanyEmploymentService(
        engine,
        SessionLocal,
        Path(get_settings().world_data_dir).resolve(),
    )
    yield engine, service
    engine._runtimes.clear()


def test_seed_apply_review_shift_and_payroll(system) -> None:
    engine, service = system
    runtime = engine.create_world("企业测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)

    companies = service.list_companies(runtime.world_id)
    assert {row["company_id"] for row in companies} == {
        "company_morning_farm",
        "company_village_shop",
    }

    openings = service.list_openings(runtime.world_id)
    farm_opening = next(row for row in openings if row["position_id"] == "position_farm_worker")
    application = service.apply(
        runtime.world_id,
        farm_opening["opening_id"],
        "agent_linxia",
        "希望获得稳定收入",
    )
    reviewed = service.review(
        runtime.world_id,
        application["application_id"],
        "agent_zhangming",
        "accept",
        "同意录用",
    )
    assert reviewed["employment_id"]

    employment_view = service.list_agent_employment(runtime.world_id, "agent_linxia")
    shift = employment_view["shifts"][0]
    assert shift["status"] == "scheduled"

    advance_minutes(engine, runtime.world_id, shift["scheduled_start"] - runtime.clock.world_time)
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": runtime.world_id, "agent_id": "agent_linxia"})
        assert agent is not None
        agent.location_id = "village_farm"
        session.commit()
    finally:
        session.close()

    started = service.start_shift(runtime.world_id, shift["shift_id"], "agent_linxia")
    assert started["status"] in {"in_progress", "late"}
    advance_minutes(engine, runtime.world_id, started["scheduled_end"] - runtime.clock.world_time)

    session = SessionLocal()
    try:
        completed = session.get(WorkShift, shift["shift_id"])
        contract = session.get(EmploymentContract, reviewed["employment_id"])
        company = session.get(
            Company,
            {"world_id": runtime.world_id, "company_id": "company_morning_farm"},
        )
        agent = session.get(Agent, {"world_id": runtime.world_id, "agent_id": "agent_linxia"})
        assert completed is not None and completed.status == "completed"
        assert completed.payroll_status == "paid"
        assert contract is not None and contract.completed_shifts == 1
        assert company is not None and company.money == 800 - completed.wage_paid
        assert agent is not None and agent.money >= 50 + completed.wage_paid
    finally:
        session.close()


def test_duplicate_application_rejected(system) -> None:
    engine, service = system
    runtime = engine.create_world("重复申请测试")
    service.ensure_seeded(runtime.world_id)
    opening = service.list_openings(runtime.world_id)[0]
    service.apply(runtime.world_id, opening["opening_id"], "agent_linxia", "第一次")
    with pytest.raises(ValueError, match="已经申请过"):
        service.apply(runtime.world_id, opening["opening_id"], "agent_linxia", "第二次")
