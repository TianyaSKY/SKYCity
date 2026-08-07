"""Company and formal-employment MVP lifecycle tests."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.companies import (
    Company,
    CompanyInventory,
    CompanyTransaction,
    EmploymentContract,
    JobOpening,
    Position,
    WorkShift,
)
from app.database.models.inventories import Inventory
from app.database.models.jobs import Job
from app.database.models.locations import WorldLocation
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.stores import Store, StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.world_events import WorldEvent
from app.database.session import SessionLocal
from app.main import app
from app.services.action_execution_service import ActionExecutionService
from app.services.company_employment_service import CompanyEmploymentService
from app.services.economy_service import EconomyService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_world_engine import advance_minutes


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def system(world_config: ParsedWorldConfig):
    engine = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    engine.action_service = ActionExecutionService(engine, SessionLocal)
    engine.economy_service = EconomyService(engine, SessionLocal)
    from app.services.god_action_service import GodActionService

    engine.god_action_service = GodActionService(engine, SessionLocal)
    service = CompanyEmploymentService(
        engine,
        SessionLocal,
        Path(get_settings().world_data_dir).resolve(),
    )
    engine.company_employment_service = service
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
        "company_village_bakery",
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
        # Money before the shift: upkeep (R20.4) may already have run at 00:00.
        money_before = agent.money
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
        # Same game day as the shift: exactly one wage credit, no extra upkeep.
        assert agent is not None and agent.money == money_before + completed.wage_paid
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


def test_seed_contract_and_queries(system) -> None:
    """E2: 世界创建后企业/岗位/招聘/商店归属/流水均可查询且符合配置."""
    engine, service = system
    runtime = engine.create_world("种子查询测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id

    companies = {row["company_id"]: row for row in service.list_companies(world_id)}
    assert set(companies) == {
        "company_morning_farm", "company_village_shop", "company_village_bakery"
    }
    assert companies["company_morning_farm"]["money"] == 800
    assert companies["company_morning_farm"]["status"] == "active"
    assert companies["company_morning_farm"]["employee_count"] == 0
    assert companies["company_morning_farm"]["open_vacancies"] == 2
    assert companies["company_village_shop"]["money"] == 1000

    farm_positions = service.list_positions(world_id, "company_morning_farm")
    worker = next(p for p in farm_positions if p["position_id"] == "position_farm_worker")
    # M16: 农场正式岗位绑定生产配方（农场生产）。
    assert worker["job_name"] == "农场生产"
    assert worker["wage_per_shift"] == 60
    assert worker["shift_start_minute"] == 480 and worker["shift_end_minute"] == 720
    assert worker["capacity"] == 2 and worker["vacancies"] == 2
    shop_positions = service.list_positions(world_id, "company_village_shop")
    attendant = next(p for p in shop_positions if p["position_id"] == "position_shop_attendant")
    assert attendant["job_name"] == "商店值班"
    assert attendant["wage_per_shift"] == 90
    assert attendant["shift_start_minute"] == 540 and attendant["shift_end_minute"] == 1020
    assert attendant["capacity"] == 1

    openings = service.list_openings(world_id)
    assert len(openings) == 3
    farm_opening = next(o for o in openings if o["company_id"] == "company_morning_farm")
    assert farm_opening["vacancies"] == 2

    # 商店归属 + 初始资金流水 + 员工列表
    session = SessionLocal()
    try:
        store = session.scalar(
            select(Store).where(Store.world_id == world_id, Store.store_id == "village_shop")
        )
        assert store is not None and store.company_id == "company_village_shop"
    finally:
        session.close()
    txs = service.list_company_transactions(world_id, "company_morning_farm")
    assert any(tx["type"] == "initial_capital" and tx["amount"] == 800 for tx in txs)
    assert service.list_employees(world_id, "company_morning_farm") == []
    assert service.list_agent_shifts(world_id, "agent_linxia") == []
    with pytest.raises(ValueError, match="企业不存在"):
        service.get_company(world_id, "company_unknown")


def test_company_query_endpoints(client: TestClient) -> None:
    """E2: 企业详情/岗位/员工/流水/班次查询端点可用."""
    world = client.post("/api/worlds", json={"name": "API 企业查询"}).json()
    world_id = world["world_id"]
    try:
        companies = client.get(f"/api/worlds/{world_id}/companies").json()
        assert {row["company_id"] for row in companies} == {
            "company_morning_farm",
            "company_village_shop",
            "company_village_bakery",
        }
        detail = client.get(f"/api/worlds/{world_id}/companies/company_morning_farm").json()
        assert detail["money"] == 800 and detail["open_vacancies"] == 2

        positions = client.get(
            f"/api/worlds/{world_id}/companies/company_village_shop/positions"
        ).json()
        assert positions[0]["wage_per_shift"] == 90

        employees = client.get(
            f"/api/worlds/{world_id}/companies/company_morning_farm/employees"
        ).json()
        assert employees == []

        txs = client.get(
            f"/api/worlds/{world_id}/companies/company_morning_farm/transactions"
        ).json()
        assert any(tx["type"] == "initial_capital" and tx["amount"] == 800 for tx in txs)

        shifts = client.get(f"/api/worlds/{world_id}/agents/agent_linxia/shifts").json()
        assert shifts == []
        missing = client.get(
            f"/api/worlds/{world_id}/companies/company_unknown/positions"
        )
        assert missing.status_code == 404
    finally:
        client.delete(f"/api/worlds/{world_id}")


def test_withdraw_application(system) -> None:
    engine, service = system
    runtime = engine.create_world("撤回申请测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    opening = service.list_openings(runtime.world_id)[0]

    application = service.apply(runtime.world_id, opening["opening_id"], "agent_linxia", "试试")
    withdrawn = service.withdraw(runtime.world_id, application["application_id"], "agent_linxia")
    assert withdrawn["status"] == "withdrawn"
    # 已撤回不能再撤回；他人不能撤回
    with pytest.raises(ValueError, match="申请已经处理"):
        service.withdraw(runtime.world_id, application["application_id"], "agent_linxia")
    application2 = service.apply(runtime.world_id, opening["opening_id"], "agent_linxia", "再试")
    with pytest.raises(ValueError, match="不能撤回他人的申请"):
        service.withdraw(runtime.world_id, application2["application_id"], "agent_wangfang")


def test_review_permissions_and_reviewed_once(system) -> None:
    engine, service = system
    runtime = engine.create_world("审核权限测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    opening = service.list_openings(runtime.world_id)[0]

    application = service.apply(runtime.world_id, opening["opening_id"], "agent_linxia", "求职")
    # 非经理不能审核（agent_wangfang 是商店经理，不是农场经理）
    with pytest.raises(ValueError, match="没有审核"):
        service.review(runtime.world_id, application["application_id"], "agent_wangfang", "accept", "录用")
    # 经理接受成功
    service.review(runtime.world_id, application["application_id"], "agent_zhangming", "accept", "录用")
    # 同一申请不能再次审核
    with pytest.raises(ValueError, match="申请已经处理"):
        service.review(runtime.world_id, application["application_id"], "agent_zhangming", "accept", "再录")


def test_last_vacancy_not_double_hired(system) -> None:
    """E3: 两个申请竞争最后一个名额，只能录用一人."""
    engine, service = system
    runtime = engine.create_world("名额竞争测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    shop_opening = next(
        o for o in service.list_openings(runtime.world_id)
        if o["company_id"] == "company_village_shop"
    )
    assert shop_opening["vacancies"] == 1
    app1 = service.apply(runtime.world_id, shop_opening["opening_id"], "agent_linxia", "第一")
    app2 = service.apply(runtime.world_id, shop_opening["opening_id"], "agent_wangfang", "第二")

    first = service.review(runtime.world_id, app1["application_id"], "agent_wangfang", "accept", "录")
    assert first["employment_id"]
    # 第二名已被占满（reject 分支先校验权限与状态，占用后 accept 应失败）
    with pytest.raises(ValueError, match="岗位已满"):
        service.review(runtime.world_id, app2["application_id"], "agent_wangfang", "accept", "也想录")
    rejected = service.review(runtime.world_id, app2["application_id"], "agent_wangfang", "reject", "满员")
    assert rejected["application"]["status"] == "rejected"


def test_one_active_contract_per_agent(system) -> None:
    """E3: 已有正式工作的居民不能再被录用（R24 冲突检查）."""
    engine, service = system
    runtime = engine.create_world("合同冲突测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    farm_opening = next(
        o for o in service.list_openings(runtime.world_id)
        if o["company_id"] == "company_morning_farm"
    )
    shop_opening = next(
        o for o in service.list_openings(runtime.world_id)
        if o["company_id"] == "company_village_shop"
    )
    app1 = service.apply(runtime.world_id, farm_opening["opening_id"], "agent_linxia", "求职")
    service.review(runtime.world_id, app1["application_id"], "agent_zhangming", "accept", "录用")
    app2 = service.apply(runtime.world_id, shop_opening["opening_id"], "agent_linxia", "想兼职")
    with pytest.raises(ValueError, match="已经有正式工作"):
        service.review(runtime.world_id, app2["application_id"], "agent_wangfang", "accept", "录用")


def test_closed_opening_cannot_apply(system) -> None:
    engine, service = system
    runtime = engine.create_world("关闭招聘测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    shop_opening = next(
        o for o in service.list_openings(runtime.world_id)
        if o["company_id"] == "company_village_shop"
    )
    app1 = service.apply(runtime.world_id, shop_opening["opening_id"], "agent_linxia", "求职")
    service.review(runtime.world_id, app1["application_id"], "agent_wangfang", "accept", "录用")
    # 满员后招聘转 filled，不能再申请
    with pytest.raises(ValueError, match="已关闭|已满"):
        service.apply(runtime.world_id, shop_opening["opening_id"], "agent_chenyu", "晚了")


def test_application_boosts_manager_decision(system) -> None:
    """E3: 提交申请后经理获得 +1 决策提升（R25）."""
    engine, service = system
    runtime = engine.create_world("经理提升测试", autonomous=True)
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    opening = service.list_openings(runtime.world_id)[0]
    service.apply(runtime.world_id, opening["opening_id"], "agent_linxia", "求职")
    manager_id = "agent_zhangming"
    session = SessionLocal()
    try:
        boosts = session.scalars(
            select(ScheduledAction).where(
                ScheduledAction.world_id == runtime.world_id,
                ScheduledAction.agent_id == manager_id,
                ScheduledAction.action_type == "agent_decide",
            )
        ).all()
        assert any(a.payload.get("origin") == "application_boost" for a in boosts)
    finally:
        session.close()


def test_observation_shows_board_and_manager_desk(system) -> None:
    """E3: 观察包含【公开招聘】；经理观察包含【企业经营】与【待审核求职申请】."""
    from app.agents.observation_service import build_observation

    engine, service = system
    runtime = engine.create_world("观察测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    opening = service.list_openings(world_id)[0]
    service.apply(world_id, opening["opening_id"], "agent_linxia", "希望获得稳定收入")

    resident_view = build_observation(world_id, "agent_linxia", SessionLocal)
    assert "【公开招聘】" in resident_view
    assert "apply_job(" in resident_view
    assert "【我的申请】" in resident_view
    manager_view = build_observation(world_id, "agent_zhangming", SessionLocal)
    assert "【企业经营】" in manager_view
    assert "【待审核求职申请】" in manager_view
    assert "review_job_application(" in manager_view


def _hire_farm_worker(service, world_id: str, agent_id: str = "agent_linxia") -> str:
    """Hire one farm worker, return the employment id (E4 helpers)."""
    farm_opening = next(
        o for o in service.list_openings(world_id)
        if o["company_id"] == "company_morning_farm"
    )
    application = service.apply(world_id, farm_opening["opening_id"], agent_id, "求职")
    reviewed = service.review(world_id, application["application_id"], "agent_zhangming", "accept", "录用")
    return reviewed["employment_id"]


def _next_shift(service, world_id: str, employment_id: str) -> dict:
    view = service.list_agent_employment(world_id, "agent_linxia")
    return next(s for s in view["shifts"] if s["employment_id"] == employment_id)


def _place_at_farm(engine, world_id: str, agent_id: str = "agent_linxia") -> None:
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        assert agent is not None
        agent.location_id = "village_farm"
        session.commit()
    finally:
        session.close()


def test_shift_start_window_and_late(system) -> None:
    """E4: 签到窗口(-30/+120)、迟到计算、窗口外拒绝."""
    engine, service = system
    runtime = engine.create_world("签到窗口测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)

    # 提前 31 分钟：拒绝（先到达工作地点）
    _place_at_farm(engine, world_id)
    advance_minutes(engine, world_id, shift["scheduled_start"] - 31 - runtime.clock.world_time)
    with pytest.raises(ValueError, match="尚未到签到时间"):
        service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    # 提前 30 分钟：允许，准时
    advance_minutes(engine, world_id, 1)
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    assert started["status"] == "in_progress"
    assert started["late_minutes"] == 0


def test_shift_start_late_and_beyond_window(system) -> None:
    engine, service = system
    runtime = engine.create_world("迟到测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)

    # 迟到 90 分钟：status=late, late_minutes=90
    advance_minutes(engine, world_id, shift["scheduled_start"] + 90 - runtime.clock.world_time)
    _place_at_farm(engine, world_id)
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    assert started["status"] == "late"
    assert started["late_minutes"] == 90
    # 完成班次（提前结束：迟到 90 后剩余 150 分钟）
    advance_minutes(engine, world_id, started["scheduled_end"] - runtime.clock.world_time)
    session = SessionLocal()
    try:
        done = session.get(WorkShift, shift["shift_id"])
        assert done is not None and done.status == "completed"
        assert done.payroll_status == "paid"
        assert done.wage_due == 60 * 150 // 240  # 比例工资
    finally:
        session.close()


def test_shift_start_rejects_beyond_window(system) -> None:
    engine, service = system
    runtime = engine.create_world("超时签到测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)
    advance_minutes(engine, world_id, shift["scheduled_start"] + 121 - runtime.clock.world_time)
    _place_at_farm(engine, world_id)
    # +120 时缺勤检查已触发，班次转 absent，签到被拒
    with pytest.raises(ValueError, match="班次不是待签到状态"):
        service.start_shift(world_id, shift["shift_id"], "agent_linxia")


def test_shift_start_wrong_location_and_busy(system) -> None:
    engine, service = system
    runtime = engine.create_world("签到状态测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)
    advance_minutes(engine, world_id, shift["scheduled_start"] - runtime.clock.world_time)
    # 不在工作地点
    with pytest.raises(ValueError, match="不在工作地点"):
        service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    _place_at_farm(engine, world_id)
    # 有进行中行动
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None
        agent.action_type = "work"
        session.commit()
    finally:
        session.close()
    with pytest.raises(ValueError, match="当前行动未完成"):
        service.start_shift(world_id, shift["shift_id"], "agent_linxia")


def test_shift_absence_auto_judged(system) -> None:
    """E4: 未签到且超时 → 缺勤（不依赖 LLM），合同记录缺勤并生成下一班次."""
    engine, service = system
    runtime = engine.create_world("缺勤测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)
    # 直接推进到 签到+121 分钟（缺勤检查在 +120 触发）
    advance_minutes(engine, world_id, shift["scheduled_start"] + 121 - runtime.clock.world_time)
    session = SessionLocal()
    try:
        done = session.get(WorkShift, shift["shift_id"])
        contract = session.get(EmploymentContract, employment_id)
        assert done is not None and done.status == "absent"
        assert done.wage_due == 0
        assert contract is not None and contract.absent_shifts == 1
        assert contract.attendance_score == 90.0
    finally:
        session.close()
    # 缺勤后自动生成下一班次
    view = service.list_agent_employment(world_id, "agent_linxia")
    assert len([s for s in view["shifts"] if s["status"] == "scheduled"]) == 1


def test_leave_flow_approved_and_rejected(system) -> None:
    engine, service = system
    runtime = engine.create_world("请假测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)

    request = service.request_leave(world_id, shift["shift_id"], "agent_linxia", "精力过低")
    assert request["status"] == "pending"
    # 重复申请同一班次被拒
    with pytest.raises(ValueError, match="已有待审批"):
        service.request_leave(world_id, shift["shift_id"], "agent_linxia", "再请")
    # 非经理不能审批
    with pytest.raises(ValueError, match="没有审批"):
        service.review_leave_request(world_id, request["request_id"], "agent_wangfang", "approve", "准")
    # 经理批准 → 班次转 leave；推进过签到窗口不判缺勤
    approved = service.review_leave_request(world_id, request["request_id"], "agent_zhangming", "approve", "好好休息")
    assert approved["status"] == "approved"
    session = SessionLocal()
    try:
        shift_row = session.get(WorkShift, shift["shift_id"])
        contract = session.get(EmploymentContract, employment_id)
        assert shift_row is not None and shift_row.status == "leave"
        assert contract is not None and contract.absent_shifts == 0
    finally:
        session.close()
    advance_minutes(engine, world_id, shift["scheduled_start"] + 121 - runtime.clock.world_time)
    session = SessionLocal()
    try:
        shift_row = session.get(WorkShift, shift["shift_id"])
        assert shift_row is not None and shift_row.status == "leave"
    finally:
        session.close()

    # 新班次（缺勤判定后生成的下一班）请假被拒 → 保持 scheduled
    view = service.list_agent_employment(world_id, "agent_linxia")
    next_shift = next(s for s in view["shifts"] if s["status"] == "scheduled")
    request2 = service.request_leave(world_id, next_shift["shift_id"], "agent_linxia", "想休息")
    service.review_leave_request(world_id, request2["request_id"], "agent_zhangming", "reject", "人手不足")
    session = SessionLocal()
    try:
        shift_row = session.get(WorkShift, next_shift["shift_id"])
        assert shift_row is not None and shift_row.status == "scheduled"
    finally:
        session.close()


def test_shift_upcoming_reminder_and_boost(system) -> None:
    """E4: 班前 60 分钟触发 shift_upcoming 提醒并提升员工决策."""
    engine, service = system
    runtime = engine.create_world("班前提醒测试", autonomous=True)
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)
    advance_minutes(engine, world_id, shift["scheduled_start"] - 60 - runtime.clock.world_time)
    session = SessionLocal()
    try:
        events = session.scalars(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.type == "shift_upcoming",
            )
        ).all()
        assert len(events) == 1
        boosts = session.scalars(
            select(ScheduledAction).where(
                ScheduledAction.world_id == world_id,
                ScheduledAction.agent_id == "agent_linxia",
                ScheduledAction.action_type == "agent_decide",
            )
        ).all()
        assert any(a.payload.get("origin") == "shift_boost" for a in boosts)
    finally:
        session.close()


def test_resign_cancels_shifts_and_reopens_opening(system) -> None:
    engine, service = system
    runtime = engine.create_world("辞职测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    view = service.list_agent_employment(world_id, "agent_linxia")
    assert view["employment"]["status"] == "active"
    resigned = service.resign(world_id, employment_id, "agent_linxia", "另有打算")
    assert resigned["status"] == "resigned"
    session = SessionLocal()
    try:
        cancelled = session.scalars(
            select(WorkShift).where(
                WorkShift.world_id == world_id,
                WorkShift.employment_id == employment_id,
                WorkShift.status == "cancelled",
            )
        ).all()
        assert len(cancelled) >= 1
        opening = session.scalar(
            select(JobOpening).where(
                JobOpening.world_id == world_id,
                JobOpening.position_id == "position_farm_worker",
            )
        )
        assert opening is not None and opening.status == "open" and opening.vacancies >= 1
    finally:
        session.close()
    # 已辞职不能再签到/请假/辞职
    session = SessionLocal()
    try:
        shift_row = session.scalars(
            select(WorkShift).where(
                WorkShift.world_id == world_id,
                WorkShift.employment_id == employment_id,
            )
        ).first()
        assert shift_row is not None
        shift_id = shift_row.shift_id
        with pytest.raises(ValueError, match="班次不是待签到状态"):
            service.start_shift(world_id, shift_id, "agent_linxia")
    finally:
        session.close()
    with pytest.raises(ValueError, match="劳动合同已经结束"):
        service.resign(world_id, employment_id, "agent_linxia", "再辞")


def test_payroll_dual_ledger_trace_and_idempotent(system) -> None:
    """E5: 工资双流水同 trace_id；重复执行完成处理器不重复发钱."""
    engine, service = system
    runtime = engine.create_world("工资流水测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)
    advance_minutes(engine, world_id, shift["scheduled_start"] - runtime.clock.world_time)
    _place_at_farm(engine, world_id)
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    advance_minutes(engine, world_id, started["scheduled_end"] - runtime.clock.world_time)

    session = SessionLocal()
    try:
        company_tx = session.scalar(
            select(CompanyTransaction).where(
                CompanyTransaction.world_id == world_id,
                CompanyTransaction.company_id == "company_morning_farm",
                CompanyTransaction.type == "wage_payment",
            )
        )
        agent_tx = session.scalar(
            select(Transaction).where(
                Transaction.world_id == world_id,
                Transaction.agent_id == "agent_linxia",
                Transaction.type == "work_wage",
            )
        )
        assert company_tx is not None and agent_tx is not None
        assert company_tx.trace_id == agent_tx.trace_id
        assert company_tx.amount == -agent_tx.amount
        assert company_tx.reference_id == shift["shift_id"]

        # 幂等：对已完成班次再次调度完成处理器 → 无任何资金变化
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        money_before_agent = agent.money
        money_before_company = company.money
        tx_count = session.scalar(
            select(func.count()).select_from(Transaction).where(
                Transaction.world_id == world_id,
                Transaction.agent_id == "agent_linxia",
                Transaction.type == "work_wage",
            )
        )
        from types import SimpleNamespace

        service.handle_shift_completed(
            session,
            SimpleNamespace(
                world_id=world_id,
                agent_id="agent_linxia",
                payload={"shift_id": shift["shift_id"]},
            ),
        )
        session.commit()
        session.refresh(agent)
        session.refresh(company)
        assert agent.money == money_before_agent
        assert company.money == money_before_company
        tx_count_after = session.scalar(
            select(func.count()).select_from(Transaction).where(
                Transaction.world_id == world_id,
                Transaction.agent_id == "agent_linxia",
                Transaction.type == "work_wage",
            )
        )
        assert tx_count_after == tx_count
    finally:
        session.close()


def test_payroll_unpaid_and_repay(system) -> None:
    """E5: 企业余额不足 → 整体欠薪不凭空发钱；资金到位后补发（含 wage_repaid）."""
    engine, service = system
    runtime = engine.create_world("欠薪测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)

    # 掏空企业资金
    session = SessionLocal()
    try:
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        assert company is not None
        company.money = 0
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        money_before = agent.money
        session.commit()
    finally:
        session.close()

    # 完成第一个班次 → 欠薪 60
    advance_minutes(engine, world_id, shift["scheduled_start"] - runtime.clock.world_time)
    _place_at_farm(engine, world_id)
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    advance_minutes(engine, world_id, started["scheduled_end"] - runtime.clock.world_time)
    session = SessionLocal()
    try:
        contract = session.get(EmploymentContract, employment_id)
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert contract is not None and contract.unpaid_wage == 60
        assert company is not None and company.unpaid_wage_total == 60
        # 没凭空发钱：没有任何正式工资流水（跨午夜的开销扣除与工资无关）
        wages = sum(
            tx.amount
            for tx in session.scalars(
                select(Transaction).where(
                    Transaction.world_id == world_id,
                    Transaction.agent_id == "agent_linxia",
                    Transaction.type == "work_wage",
                )
            ).all()
        )
        assert wages == 0
        events = session.scalars(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.type == "wage_unpaid",
            )
        ).all()
        assert len(events) == 1
    finally:
        session.close()

    # 上帝注资 200，完成第二个班次 → 当期工资 60 付清 + 补发欠薪 60
    session = SessionLocal()
    try:
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        company.money += 200
        session.commit()
    finally:
        session.close()
    view = service.list_agent_employment(world_id, "agent_linxia")
    shift2 = next(s for s in view["shifts"] if s["status"] == "scheduled")
    advance_minutes(engine, world_id, shift2["scheduled_start"] - runtime.clock.world_time)
    _place_at_farm(engine, world_id)
    started2 = service.start_shift(world_id, shift2["shift_id"], "agent_linxia")
    advance_minutes(engine, world_id, started2["scheduled_end"] - runtime.clock.world_time)

    session = SessionLocal()
    try:
        contract = session.get(EmploymentContract, employment_id)
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        assert contract is not None and contract.unpaid_wage == 0
        assert company is not None and company.unpaid_wage_total == 0
        assert company.money == 200 - 120  # 班次2工资60 + 补发60
        wages = sum(
            tx.amount
            for tx in session.scalars(
                select(Transaction).where(
                    Transaction.world_id == world_id,
                    Transaction.agent_id == "agent_linxia",
                    Transaction.type == "work_wage",
                )
            ).all()
        )
        assert wages == 120
        repaid = session.scalars(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.type == "wage_repaid",
            )
        ).all()
        assert len(repaid) == 1
    finally:
        session.close()


def test_formal_work_products_enter_company_inventory(system) -> None:
    """E6: 正式班次产物进企业库存，不进个人背包（R30）."""
    engine, service = system
    runtime = engine.create_world("产物入库测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)
    advance_minutes(engine, world_id, shift["scheduled_start"] - runtime.clock.world_time)
    _place_at_farm(engine, world_id)
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    advance_minutes(engine, world_id, started["scheduled_end"] - runtime.clock.world_time)

    session = SessionLocal()
    try:
        inventory = session.get(
            CompanyInventory, {"world_id": world_id, "company_id": "company_morning_farm", "item_id": "wheat"}
        )
        # M16: the farm position now runs the production recipe (10 wheat/shift).
        assert inventory is not None and inventory.quantity == 10
        agent_inv = session.get(
            Inventory, {"world_id": world_id, "agent_id": "agent_linxia", "item_id": "wheat"}
        )
        assert agent_inv is None
    finally:
        session.close()


def test_store_sale_credits_company(system) -> None:
    """E6: 居民购买 → 商店库存减、居民扣钱、企业余额增（同一事务）."""
    engine, service = system
    runtime = engine.create_world("商店入账测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None
        agent.location_id = "village_shop"
        session.commit()
    finally:
        session.close()

    ok, _, reason = engine.economy_service.buy(world_id, "agent_linxia", "bread", quantity=1, reason="买面包")
    assert ok is True and reason is None
    session = SessionLocal()
    try:
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_village_shop"}
        )
        assert company is not None and company.money == 1000 + 12
        tx = session.scalar(
            select(CompanyTransaction).where(
                CompanyTransaction.world_id == world_id,
                CompanyTransaction.company_id == "company_village_shop",
                CompanyTransaction.type == "sale_income",
            )
        )
        assert tx is not None and tx.amount == 12
        events = session.scalars(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.type == "company_sale_completed",
            )
        ).all()
        assert len(events) == 1
        store_row = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert store_row is not None and store_row.stock == 20 - 1
    finally:
        session.close()


def test_store_purchase_debits_company_and_rejects_when_broke(system) -> None:
    """E6: 商店收购从企业账户支付；企业资金不足拒绝且库存不变."""
    engine, service = system
    runtime = engine.create_world("商店扣款测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None
        agent.location_id = "village_shop"
        session.add(Inventory(world_id=world_id, agent_id="agent_linxia", item_id="apple", quantity=3))
        store_row = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "apple"}
        )
        assert store_row is not None
        store_row.stock = 14  # 留出收购空间（上限 15）
        session.commit()
    finally:
        session.close()

    ok, _, reason = engine.economy_service.sell(world_id, "agent_linxia", "apple", quantity=1, reason="卖苹果")
    assert ok is True and reason is None
    session = SessionLocal()
    try:
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_village_shop"}
        )
        assert company is not None and company.money == 1000 - 3
        # 掏空企业资金 → 收购被拒
        company.money = 2
        session.commit()
    finally:
        session.close()
    ok, _, reason = engine.economy_service.sell(world_id, "agent_linxia", "apple", quantity=1, reason="再卖")
    assert ok is False and reason == "企业资金不足"
    session = SessionLocal()
    try:
        store_row = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "apple"}
        )
        assert store_row is not None and store_row.stock == 14 + 1  # 只有第一次成功
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None and agent.money == 50 + 3  # 第二次失败未入账
    finally:
        session.close()


def test_terminate_employment(system) -> None:
    """E7: 经理解雇：权限、班次取消、名额恢复、欠薪保留."""
    engine, service = system
    runtime = engine.create_world("解雇测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)

    # 非经理不能解雇
    with pytest.raises(ValueError, match="没有解雇"):
        service.terminate(world_id, employment_id, "agent_wangfang", "辞退")
    # 经理解雇
    terminated = service.terminate(world_id, employment_id, "agent_zhangming", "表现不佳")
    assert terminated["status"] == "terminated"
    # 重复终止被拒
    with pytest.raises(ValueError, match="劳动合同已经结束"):
        service.terminate(world_id, employment_id, "agent_zhangming", "再解雇")
    session = SessionLocal()
    try:
        shifts = session.scalars(
            select(WorkShift).where(
                WorkShift.world_id == world_id,
                WorkShift.employment_id == employment_id,
            )
        ).all()
        assert all(s.status == "cancelled" for s in shifts)
        opening = session.scalar(
            select(JobOpening).where(
                JobOpening.world_id == world_id,
                JobOpening.position_id == "position_farm_worker",
            )
        )
        assert opening is not None and opening.status == "open" and opening.vacancies >= 1
    finally:
        session.close()


def test_pause_resume_recruitment(system) -> None:
    engine, service = system
    runtime = engine.create_world("招聘暂停测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    # 非经理不能暂停
    with pytest.raises(ValueError, match="没有管理"):
        service.pause_recruitment(world_id, "position_farm_worker", "agent_wangfang")
    opening = next(
        o for o in service.list_openings(world_id)
        if o["position_id"] == "position_farm_worker"
    )
    service.pause_recruitment(world_id, "position_farm_worker", "agent_zhangming")
    # 暂停后招聘从公开列表消失，申请被拒
    assert "position_farm_worker" not in {
        o["position_id"] for o in service.list_openings(world_id)
    }
    with pytest.raises(ValueError, match="已关闭|已满"):
        service.apply(world_id, opening["opening_id"], "agent_linxia", "求职")
    service.resume_recruitment(world_id, "position_farm_worker", "agent_zhangming")
    reopened = next(
        o for o in service.list_openings(world_id)
        if o["position_id"] == "position_farm_worker"
    )
    assert reopened["vacancies"] == 2
    application = service.apply(world_id, reopened["opening_id"], "agent_linxia", "求职")
    assert application["status"] == "submitted"


def test_suspend_resume_company(system) -> None:
    """E7: 停业取消未来班次并停止招聘；恢复后班次与招聘恢复."""
    engine, service = system
    runtime = engine.create_world("停业测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)

    # 非经理不能停业
    with pytest.raises(ValueError, match="没有管理"):
        service.suspend_company(world_id, "company_morning_farm", "agent_wangfang", "停")
    suspended = service.suspend_company(world_id, "company_morning_farm", "agent_zhangming", "整顿")
    assert suspended["status"] == "suspended"
    session = SessionLocal()
    try:
        # 未来班次全部取消
        shift_row = session.get(WorkShift, shift["shift_id"])
        assert shift_row is not None and shift_row.status == "cancelled"
        # 招聘暂停
        opening = session.scalar(
            select(JobOpening).where(
                JobOpening.world_id == world_id,
                JobOpening.position_id == "position_farm_worker",
            )
        )
        assert opening is not None and opening.status == "paused"
    finally:
        session.close()
    # 停业中不能签到
    with pytest.raises(ValueError, match="班次不是待签到状态|企业未在经营"):
        service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    # 停业中不能申请（招聘已暂停）
    with pytest.raises(ValueError, match="已关闭|已满"):
        service.apply(world_id, opening.opening_id, "agent_linxia", "求职")

    resumed = service.resume_company(world_id, "company_morning_farm", "agent_zhangming", "恢复")
    assert resumed["status"] == "active"
    view = service.list_agent_employment(world_id, "agent_linxia")
    new_shift = next(s for s in view["shifts"] if s["status"] == "scheduled")
    assert new_shift["shift_id"] != shift["shift_id"]
    openings = service.list_openings(world_id)
    assert any(o["position_id"] == "position_farm_worker" for o in openings)


def test_god_inject_company_money_repays(system) -> None:
    """E7: 上帝注资入企业账户，并立即补发欠薪（wage_repaid）."""
    engine, service = system
    runtime = engine.create_world("注资测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)

    session = SessionLocal()
    try:
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        assert company is not None
        company.money = 0
        session.commit()
    finally:
        session.close()
    # 完成班次 → 欠薪 60
    advance_minutes(engine, world_id, shift["scheduled_start"] - runtime.clock.world_time)
    _place_at_farm(engine, world_id)
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    advance_minutes(engine, world_id, started["scheduled_end"] - runtime.clock.world_time)

    god = engine.god_action_service
    if god is None:  # 测试环境未装配上帝服务时直接验证服务层
        session = SessionLocal()
        try:
            company = session.get(
                Company, {"world_id": world_id, "company_id": "company_morning_farm"}
            )
            company.money += 200
            session.commit()
        finally:
            session.close()
    else:
        outcome = god.apply(
            world_id, "inject_company_money", "company_morning_farm",
            {"amount": 200}, "补发工资",
        )
        assert outcome["result"]["repaid_total"] == 60
    session = SessionLocal()
    try:
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        contract = session.get(EmploymentContract, employment_id)
        assert company is not None and company.money == 200 - 60
        assert company.unpaid_wage_total == 0
        assert contract is not None and contract.unpaid_wage == 0
        repaid = session.scalars(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.type == "wage_repaid",
            )
        ).all()
        assert len(repaid) == 1
    finally:
        session.close()


def test_save_restore_v2_company_state(system) -> None:
    """E8: V2 存档保持进行中班次/欠薪/未来班次；恢复后缺勤检查与工资幂等."""
    from app.services.save_service import SaveService

    engine, service = system
    runtime = engine.create_world("V2存档测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    save_service = SaveService(engine, SessionLocal)

    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)
    # 班次1开始（进行中）；掏空企业 → 完成时欠薪
    advance_minutes(engine, world_id, shift["scheduled_start"] - runtime.clock.world_time)
    _place_at_farm(engine, world_id)
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    session = SessionLocal()
    try:
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        assert company is not None
        company.money = 0
        session.commit()
    finally:
        session.close()
    saved = save_service.save(world_id)

    runtime2 = save_service.restore(saved.save_id)
    new_world_id = runtime2.world_id
    assert new_world_id != world_id
    # 恢复后：合同/班次/欠薪/企业状态保持
    session = SessionLocal()
    try:
        contract = session.scalar(
            select(EmploymentContract).where(
                EmploymentContract.world_id == new_world_id,
                EmploymentContract.agent_id == "agent_linxia",
                EmploymentContract.status == "active",
            )
        )
        assert contract is not None
        shift_row = session.scalar(
            select(WorkShift).where(
                WorkShift.world_id == new_world_id,
                WorkShift.status.in_(("in_progress", "late")),
            )
        )
        assert shift_row is not None
        company = session.get(
            Company, {"world_id": new_world_id, "company_id": "company_morning_farm"}
        )
        assert company is not None and company.money == 0
        agent = session.get(Agent, {"world_id": new_world_id, "agent_id": "agent_linxia"})
        assert agent is not None and agent.action_type == "formal_work"
        max_seq_after = session.scalar(
            select(func.max(WorldEvent.sequence)).where(
                WorldEvent.world_id == new_world_id
            )
        )
    finally:
        session.close()
    # 恢复后完成班次1 → 欠薪 60（不重复支付、不凭空发钱）
    advance_minutes(engine, new_world_id, started["scheduled_end"] - runtime2.clock.world_time)
    session = SessionLocal()
    try:
        contract = session.scalar(
            select(EmploymentContract).where(
                EmploymentContract.world_id == new_world_id,
                EmploymentContract.agent_id == "agent_linxia",
                EmploymentContract.status == "active",
            )
        )
        assert contract is not None and contract.unpaid_wage == 60
        wages = sum(
            tx.amount
            for tx in session.scalars(
                select(Transaction).where(
                    Transaction.world_id == new_world_id,
                    Transaction.agent_id == "agent_linxia",
                    Transaction.type == "work_wage",
                )
            ).all()
        )
        assert wages == 0
        inventory = session.get(
            CompanyInventory,
            {"world_id": new_world_id, "company_id": "company_morning_farm", "item_id": "wheat"},
        )
        assert inventory is not None and inventory.quantity == 10
        max_seq_now = session.scalar(
            select(func.max(WorldEvent.sequence)).where(
                WorldEvent.world_id == new_world_id
            )
        )
        assert max_seq_now > max_seq_after  # sequence 连续递增
    finally:
        session.close()
    # 恢复后完成班次会生成下一班次；其缺勤检查仍触发（shift_id 已重映射）
    view = service.list_agent_employment(new_world_id, "agent_linxia")
    next_shift = next(s for s in view["shifts"] if s["status"] == "scheduled")
    advance_minutes(
        engine, new_world_id,
        next_shift["scheduled_start"] + 121 - runtime2.clock.world_time,
    )
    session = SessionLocal()
    try:
        absent = session.scalars(
            select(WorkShift).where(
                WorkShift.world_id == new_world_id,
                WorkShift.status == "absent",
            )
        ).all()
        assert len(absent) == 1
    finally:
        session.close()


def test_restore_v1_save_migrates(system) -> None:
    """E8: V1 存档迁移 — 保留旧数据、按种子重建企业并绑定商店."""
    from app.database.models.saves import Save
    from app.services.save_service import SaveService

    engine, service = system
    runtime = engine.create_world("V1迁移测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    save_service = SaveService(engine, SessionLocal)
    saved = save_service.save(runtime.world_id)

    session = SessionLocal()
    try:
        row = session.get(Save, saved.save_id)
        assert row is not None
        payload = row.payload_json
        for key in (
                "companies", "positions", "job_openings", "job_applications",
                "employment_contracts", "work_shifts", "leave_requests",
                "company_inventories", "company_transactions",
        ):
            payload.pop(key, None)
        payload["schema_version"] = 1
        row.payload_json = payload
        session.commit()
    finally:
        session.close()

    runtime2 = save_service.restore(saved.save_id)
    new_world_id = runtime2.world_id
    companies = service.list_companies(new_world_id)
    assert {row["company_id"] for row in companies} == {
        "company_morning_farm",
        "company_village_shop",
        "company_village_bakery",
    }
    assert companies[0]["money"] in {800, 1000}
    openings = service.list_openings(new_world_id)
    assert len(openings) == 3
    session = SessionLocal()
    try:
        store = session.scalar(
            select(Store).where(
                Store.world_id == new_world_id,
                Store.store_id == "village_shop",
            )
        )
        assert store is not None and store.company_id == "company_village_shop"
    finally:
        session.close()


def test_first_version_acceptance_script(system) -> None:
    """§20 第一版最终验收剧本（确定性版）：招聘→录用→排班→出勤→工资→销售→存档."""
    from app.services.save_service import SaveService

    engine, service = system
    runtime = engine.create_world("最终验收")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id

    # 1-3. 两家企业独立资金；农场 2 岗、商店 1 岗；招聘发布
    companies = {c["company_id"]: c for c in service.list_companies(world_id)}
    assert companies["company_morning_farm"]["money"] == 800
    assert companies["company_village_shop"]["money"] == 1000
    farm_worker = next(
        p for p in service.list_positions(world_id, "company_morning_farm")
        if p["position_id"] == "position_farm_worker"
    )
    assert farm_worker["capacity"] == 2
    shop_attendant = service.list_positions(world_id, "company_village_shop")[0]
    assert shop_attendant["capacity"] == 1
    assert len(service.list_openings(world_id)) == 3

    # 4-6. 居民申请 → 经理录用 → 正式职业 + 下一班次
    farm_opening = next(
        o for o in service.list_openings(world_id)
        if o["company_id"] == "company_morning_farm"
    )
    app1 = service.apply(world_id, farm_opening["opening_id"], "agent_linxia", "希望获得稳定收入")
    app2 = service.apply(world_id, farm_opening["opening_id"], "agent_chenyu", "想种田")
    rev1 = service.review(world_id, app1["application_id"], "agent_zhangming", "accept", "录用")
    rev2 = service.review(world_id, app2["application_id"], "agent_zhangming", "accept", "录用")
    emp1 = rev1["employment_id"]
    emp2 = rev2["employment_id"]
    view1 = service.list_agent_employment(world_id, "agent_linxia")
    view2 = service.list_agent_employment(world_id, "agent_chenyu")
    shift1 = next(s for s in view1["shifts"] if s["status"] == "scheduled")
    shift2 = next(s for s in view2["shifts"] if s["status"] == "scheduled")

    # 7-9. 班前提醒触发；一人准时上班、一人缺勤
    advance_minutes(engine, world_id, shift1["scheduled_start"] - 60 - runtime.clock.world_time)
    session = SessionLocal()
    try:
        reminders = session.scalars(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.type == "shift_upcoming",
            )
        ).all()
        assert len(reminders) >= 1
    finally:
        session.close()
    advance_minutes(engine, world_id, 60)
    session = SessionLocal()
    try:
        for agent_id in ("agent_linxia", "agent_chenyu"):
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            assert agent is not None
            agent.location_id = "village_farm"
        session.commit()
    finally:
        session.close()
    started1 = service.start_shift(world_id, shift1["shift_id"], "agent_linxia")  # 准时
    # 陈宇不去 → 缺勤
    advance_minutes(engine, world_id, shift1["scheduled_start"] + 121 - runtime.clock.world_time)
    session = SessionLocal()
    try:
        absent = session.get(WorkShift, shift2["shift_id"])
        assert absent is not None and absent.status == "absent"
    finally:
        session.close()

    # 10-11. 工作完成：产物进企业、工资从企业账户支付
    advance_minutes(engine, world_id, started1["scheduled_end"] - runtime.clock.world_time)
    session = SessionLocal()
    try:
        inventory = session.get(
            CompanyInventory,
            {"world_id": world_id, "company_id": "company_morning_farm", "item_id": "wheat"},
        )
        assert inventory is not None and inventory.quantity >= 1
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        assert company is not None and company.money == 800 - 60
        contract1 = session.scalar(
            select(EmploymentContract).where(
                EmploymentContract.world_id == world_id,
                EmploymentContract.employment_id == emp1,
            )
        )
        assert contract1 is not None and contract1.completed_shifts == 1
    finally:
        session.close()

    # 12. 居民购物 → 销售款进企业
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None
        agent.location_id = "village_shop"
        session.commit()
    finally:
        session.close()
    ok, _, reason = engine.economy_service.buy(world_id, "agent_linxia", "bread", quantity=1, reason="买面包")
    assert ok is True and reason is None
    session = SessionLocal()
    try:
        shop = session.get(
            Company, {"world_id": world_id, "company_id": "company_village_shop"}
        )
        assert shop is not None and shop.money == 1000 + 12
    finally:
        session.close()

    # 13-14. 企业资金不足 → 欠薪；员工看到欠薪后可辞职（欠薪保留）
    session = SessionLocal()
    try:
        company = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        assert company is not None
        company.money = 0
        session.commit()
    finally:
        session.close()
    view1 = service.list_agent_employment(world_id, "agent_linxia")
    shift3 = next(s for s in view1["shifts"] if s["status"] == "scheduled")
    advance_minutes(engine, world_id, shift3["scheduled_start"] - runtime.clock.world_time)
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None
        agent.location_id = "village_farm"
        session.commit()
    finally:
        session.close()
    started3 = service.start_shift(world_id, shift3["shift_id"], "agent_linxia")
    advance_minutes(engine, world_id, started3["scheduled_end"] - runtime.clock.world_time)
    session = SessionLocal()
    try:
        contract1 = session.scalar(
            select(EmploymentContract).where(
                EmploymentContract.world_id == world_id,
                EmploymentContract.employment_id == emp1,
            )
        )
        assert contract1 is not None and contract1.unpaid_wage == 60
    finally:
        session.close()
    resigned = service.resign(world_id, emp1, "agent_linxia", "欠薪太久")
    assert resigned["status"] == "resigned"
    session = SessionLocal()
    try:
        contract1 = session.scalar(
            select(EmploymentContract).where(
                EmploymentContract.world_id == world_id,
                EmploymentContract.employment_id == emp1,
            )
        )
        assert contract1 is not None and contract1.unpaid_wage == 60  # 欠薪保留
    finally:
        session.close()

    # 15. 存档恢复：企业、合同、班次、资金、欠薪完全保持
    save_service = SaveService(engine, SessionLocal)
    saved = save_service.save(world_id)
    runtime2 = save_service.restore(saved.save_id)
    new_world_id = runtime2.world_id
    session = SessionLocal()
    try:
        restored_company = session.get(
            Company, {"world_id": new_world_id, "company_id": "company_morning_farm"}
        )
        assert restored_company is not None and restored_company.money == 0
        assert restored_company.unpaid_wage_total == 60
        restored_contract = session.scalar(
            select(EmploymentContract).where(
                EmploymentContract.world_id == new_world_id,
                EmploymentContract.agent_id == "agent_linxia",
                EmploymentContract.status == "resigned",
            )
        )
        assert restored_contract is not None and restored_contract.unpaid_wage == 60
        restored_shifts = session.scalars(
            select(WorkShift).where(WorkShift.world_id == new_world_id)
        ).all()
        assert any(s.status == "completed" for s in restored_shifts)
        assert any(s.status == "absent" for s in restored_shifts)
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# M16: 农场→面包坊→商店 生产链（R36/R37/R38）
# --------------------------------------------------------------------------- #

def _hire(system, world_id: str, company_id: str, applicant: str, manager: str) -> str:
    """Hire one worker at a company, return the employment id."""
    engine, service = system
    opening = next(
        o for o in service.list_openings(world_id)
        if o["company_id"] == company_id
    )
    application = service.apply(world_id, opening["opening_id"], applicant, "求职")
    reviewed = service.review(
        world_id, application["application_id"], manager, "accept", "录用"
    )
    return reviewed["employment_id"]


def test_m16_seed_companies_positions_and_formal_jobs(system) -> None:
    """M16 种子：3 家企业 3 个岗位；正式岗位绑定生产配方；面包坊经理为 agent_touzi."""
    engine, service = system
    runtime = engine.create_world("M16种子测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    session = SessionLocal()
    try:
        companies = session.scalars(
            select(Company).where(Company.world_id == world_id)
        ).all()
        assert {row.company_id for row in companies} == {
            "company_morning_farm", "company_village_shop", "company_village_bakery",
        }
        bakery = next(
            row for row in companies if row.company_id == "company_village_bakery"
        )
        assert bakery.manager_agent_id == "agent_touzi"
        positions = session.scalars(
            select(Position).where(Position.world_id == world_id)
        ).all()
        assert {row.position_id for row in positions} == {
            "position_farm_worker", "position_shop_attendant", "position_baker",
        }
        farm = next(
            row for row in positions if row.position_id == "position_farm_worker"
        )
        assert farm.job_id == "job_farm_production"
        baker = next(row for row in positions if row.position_id == "position_baker")
        assert baker.job_id == "job_bakery_bake"
        job_ids = set(
            session.scalars(select(Job.job_id).where(Job.world_id == world_id)).all()
        )
        assert {"job_farm_production", "job_bakery_bake"} <= job_ids
        bakery_loc = session.get(
            WorldLocation, {"world_id": world_id, "location_id": "village_bakery"}
        )
        assert bakery_loc is not None and bakery_loc.location_type == "workshop"
    finally:
        session.close()


def test_m16_purchase_chain_ledger_and_events(system) -> None:
    """M16 R36：农场一班产 10 wheat → 面包坊采购 → 双流水/双事件/trace 共享."""
    engine, service = system
    runtime = engine.create_world("M16采购测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    # 农场正式班次产 10 wheat（工资 60 → 农场 740）
    employment_id = _hire_farm_worker(service, world_id)
    shift = _next_shift(service, world_id, employment_id)
    advance_minutes(engine, world_id, shift["scheduled_start"] - runtime.clock.world_time)
    _place_at_farm(engine, world_id)
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    advance_minutes(engine, world_id, started["scheduled_end"] - runtime.clock.world_time)
    session = SessionLocal()
    try:
        farm = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        wheat = session.get(
            CompanyInventory,
            {"world_id": world_id, "company_id": "company_morning_farm", "item_id": "wheat"},
        )
        assert wheat is not None and wheat.quantity == 10
        assert farm is not None and farm.money == 740
    finally:
        session.close()

    result = service.purchase_company_goods(
        world_id,
        "company_village_bakery",
        "company_morning_farm",
        "agent_touzi",
        "wheat",
        quantity=10,
        reason="备料",
        trace_id="trc_m16_purchase",
    )
    assert result["total"] == 60 and result["buyer_balance"] == 240

    session = SessionLocal()
    try:
        bakery = session.get(
            Company, {"world_id": world_id, "company_id": "company_village_bakery"}
        )
        farm = session.get(
            Company, {"world_id": world_id, "company_id": "company_morning_farm"}
        )
        assert bakery is not None and bakery.money == 300 - 60
        assert farm is not None and farm.money == 740 + 60
        buyer_tx = session.scalar(
            select(CompanyTransaction).where(
                CompanyTransaction.world_id == world_id,
                CompanyTransaction.company_id == "company_village_bakery",
                CompanyTransaction.type == "material_purchase",
            )
        )
        assert buyer_tx is not None
        assert buyer_tx.amount == -60 and buyer_tx.balance_after == 240
        assert buyer_tx.reference_type == "company"
        assert buyer_tx.reference_id == "company_morning_farm"
        seller_tx = session.scalar(
            select(CompanyTransaction).where(
                CompanyTransaction.world_id == world_id,
                CompanyTransaction.company_id == "company_morning_farm",
                CompanyTransaction.type == "wholesale_sale",
            )
        )
        assert seller_tx is not None
        assert seller_tx.amount == 60 and seller_tx.balance_after == 800
        assert seller_tx.reference_id == "company_village_bakery"
        events = session.scalars(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.trace_id == "trc_m16_purchase",
            )
        ).all()
        money_events = [e for e in events if e.type == "company_money_changed"]
        assert len(money_events) == 2
        assert sorted(e.payload["amount"] for e in money_events) == [-60, 60]
        purchases = [e for e in events if e.type == "company_purchase_completed"]
        assert len(purchases) == 1
        assert purchases[0].payload == {
            "company_id": "company_village_bakery",
            "seller_company_id": "company_morning_farm",
            "item_id": "wheat",
            "quantity": 10,
            "unit_price": 6,
            "total": 60,
        }
        inventory_events = [e for e in events if e.type == "company_inventory_changed"]
        assert len(inventory_events) == 2
    finally:
        session.close()


def test_m16_shift_reserves_and_consumes_inputs(system) -> None:
    """M16 R37：无原料签到被拒且无完成回调；有原料预留=10；完成后消耗并产出 20 bread."""
    engine, service = system
    runtime = engine.create_world("M16预留测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    employment_id = _hire(system, world_id, "company_village_bakery", "agent_chenyu", "agent_touzi")
    view = service.list_agent_employment(world_id, "agent_chenyu")
    shift = next(s for s in view["shifts"] if s["employment_id"] == employment_id)
    # 面包坊班次当天 13:00（780）；先到地点
    advance_minutes(engine, world_id, shift["scheduled_start"] - 30 - runtime.clock.world_time)
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_chenyu"})
        assert agent is not None
        agent.location_id = "village_bakery"
        session.commit()
    finally:
        session.close()
    # 无小麦 → 拒绝签到；班次保持 scheduled；无完成回调
    with pytest.raises(ValueError, match="生产原料不足"):
        service.start_shift(world_id, shift["shift_id"], "agent_chenyu")
    session = SessionLocal()
    try:
        row = session.get(WorkShift, shift["shift_id"])
        assert row is not None and row.status == "scheduled"
        completion = session.scalar(
            select(ScheduledAction).where(
                ScheduledAction.world_id == world_id,
                ScheduledAction.action_type == "formal_shift_completed",
                ScheduledAction.agent_id == "agent_chenyu",
            )
        )
        assert completion is None
    finally:
        session.close()
    # 备料 10 wheat → 签到成功，预留 10
    session = SessionLocal()
    try:
        session.add(
            CompanyInventory(
                world_id=world_id, company_id="company_village_bakery",
                item_id="wheat", quantity=10,
            )
        )
        session.commit()
    finally:
        session.close()
    started = service.start_shift(world_id, shift["shift_id"], "agent_chenyu")
    assert started["status"] in {"in_progress", "late"}
    session = SessionLocal()
    try:
        wheat = session.get(
            CompanyInventory,
            {"world_id": world_id, "company_id": "company_village_bakery", "item_id": "wheat"},
        )
        assert wheat is not None
        assert wheat.quantity == 10 and wheat.reserved_quantity == 10
    finally:
        session.close()
    # 完成班次：消耗 10 wheat、产出 20 bread、工资 60
    advance_minutes(engine, world_id, started["scheduled_end"] - runtime.clock.world_time)
    session = SessionLocal()
    try:
        wheat = session.get(
            CompanyInventory,
            {"world_id": world_id, "company_id": "company_village_bakery", "item_id": "wheat"},
        )
        assert wheat is not None
        assert wheat.quantity == 0 and wheat.reserved_quantity == 0
        bread = session.get(
            CompanyInventory,
            {"world_id": world_id, "company_id": "company_village_bakery", "item_id": "bread"},
        )
        assert bread is not None and bread.quantity == 20
        wage = session.scalar(
            select(Transaction).where(
                Transaction.world_id == world_id,
                Transaction.agent_id == "agent_chenyu",
                Transaction.type == "work_wage",
            )
        )
        assert wage is not None and wage.amount == 60
        production = session.scalar(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.type == "company_production_completed",
            )
        )
        assert production is not None
        assert production.payload["company_id"] == "company_village_bakery"
        assert production.payload["shift_id"] == shift["shift_id"]
        assert production.payload["consumed"] == [{"item_id": "wheat", "quantity": 10}]
        assert production.payload["products"] == [{"item_id": "bread", "quantity": 20}]
    finally:
        session.close()


def test_m16_formal_only_jobs_reject_casual_work(system) -> None:
    """M16：formal_only 的 job 拒绝 work()；旧临时岗 job_farm_field 行为不变."""
    engine, service = system
    runtime = engine.create_world("M16正式门测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None
        agent.location_id = "village_farm"
        session.commit()
    finally:
        session.close()
    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_production", reason="干农活"
    )
    assert ok is False and reason == "该工作仅限正式员工班次"
    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干农活"
    )
    assert ok is True and reason is None
    advance_minutes(engine, world_id, 121)  # 120 分钟工作 + 结算
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None and agent.action_type is None
        wheat = session.get(
            Inventory, {"world_id": world_id, "agent_id": "agent_linxia", "item_id": "wheat"}
        )
        assert wheat is not None and wheat.quantity == 1  # 旧路径 wheat×1 入个人背包
    finally:
        session.close()


def test_m16_stock_store_moves_warehouse_to_shelf(system) -> None:
    """M16 R38：经理上架仓库→货架；权限/容量/库存校验."""
    engine, service = system
    runtime = engine.create_world("M16上架测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    session = SessionLocal()
    try:
        session.add(
            CompanyInventory(
                world_id=world_id, company_id="company_village_shop",
                item_id="bread", quantity=30,
            )
        )
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product is not None
        product.stock = 0
        session.commit()
    finally:
        session.close()
    # 非本企业经理被拒
    with pytest.raises(ValueError, match="没有管理该企业上架的权限"):
        service.stock_store(
            world_id, "company_village_shop", "village_shop", "agent_touzi",
            "bread", quantity=1,
        )
    result = service.stock_store(
        world_id, "company_village_shop", "village_shop", "agent_wangfang",
        "bread", quantity=20, reason="补货",
    )
    assert result["stock_after"] == 20
    # 货架满 → 拒；仓库不足 → 拒
    with pytest.raises(ValueError, match="货架放不下"):
        service.stock_store(
            world_id, "company_village_shop", "village_shop", "agent_wangfang",
            "bread", quantity=1,
        )
    with pytest.raises(ValueError, match="企业仓库库存不足"):
        service.stock_store(
            world_id, "company_village_shop", "village_shop", "agent_wangfang",
            "bread", quantity=20,
        )
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product is not None and product.stock == 20
        warehouse = session.get(
            CompanyInventory,
            {"world_id": world_id, "company_id": "company_village_shop", "item_id": "bread"},
        )
        assert warehouse is not None and warehouse.quantity == 10
        stocked = session.scalar(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.type == "company_store_stocked",
            )
        )
        assert stocked is not None
        assert stocked.payload["quantity"] == 20 and stocked.payload["stock_after"] == 20
    finally:
        session.close()


def test_m16_retail_income_after_stocking(system) -> None:
    """M16：上架后的 bread 由居民购买，收入进杂货店企业账户（R33 扩展）."""
    engine, service = system
    runtime = engine.create_world("M16零售测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    session = SessionLocal()
    try:
        session.add(
            CompanyInventory(
                world_id=world_id, company_id="company_village_shop",
                item_id="bread", quantity=20,
            )
        )
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product is not None
        product.stock = 0
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None
        agent.location_id = "village_shop"
        session.commit()
    finally:
        session.close()
    service.stock_store(
        world_id, "company_village_shop", "village_shop", "agent_wangfang",
        "bread", quantity=20, reason="补货",
    )
    ok, _, reason = engine.economy_service.buy(
        world_id, "agent_linxia", "bread", quantity=1, reason="买面包"
    )
    assert ok is True and reason is None
    session = SessionLocal()
    try:
        shop = session.get(
            Company, {"world_id": world_id, "company_id": "company_village_shop"}
        )
        assert shop is not None and shop.money == 1000 + 12
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product is not None and product.stock == 19
        warehouse = session.get(
            CompanyInventory,
            {"world_id": world_id, "company_id": "company_village_shop", "item_id": "bread"},
        )
        assert warehouse is not None and warehouse.quantity == 0
    finally:
        session.close()


def test_m16_bread_does_not_auto_restock(system) -> None:
    """M16：bread restock_daily=0 → 推进一天货架不回补、无 bread 补货事件."""
    engine, service = system
    runtime = engine.create_world("M16不补货测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product is not None
        assert product.restock_daily == 0
        assert product.stock == 20  # 无 initial_stock → 播种即满
        product.stock = 3
        session.commit()
    finally:
        session.close()
    advance_minutes(engine, world_id, 1440)  # 次日 08:00（补货点）
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct, {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"}
        )
        assert product is not None and product.stock == 3  # 不回补
    finally:
        session.close()
    for event in engine.events_after(world_id, 0):
        if event.type == "store_restocked":
            assert all(
                item["item_id"] != "bread" for item in event.payload.get("restocked", [])
            )


def test_m16_concurrent_purchase_exactly_one_wins(system) -> None:
    """M16 R36 并发：最后 10 wheat 双采购，恰一成功且卖方库存不为负."""
    engine, service = system
    runtime = engine.create_world("M16并发采购测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    session = SessionLocal()
    try:
        session.add(
            CompanyInventory(
                world_id=world_id, company_id="company_morning_farm",
                item_id="wheat", quantity=10,
            )
        )
        session.commit()
    finally:
        session.close()
    results: list[tuple[bool, str]] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        barrier.wait()  # 两线程同时发起采购
        try:
            service.purchase_company_goods(
                world_id,
                "company_village_bakery",
                "company_morning_farm",
                "agent_touzi",
                "wheat",
                quantity=10,
                reason="抢货",
            )
            results.append((True, ""))
        except ValueError as exc:
            results.append((False, str(exc)))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert sum(1 for ok, _ in results if ok) == 1, f"expected one winner, got {results}"
    assert sum(1 for ok, reason in results if not ok and reason == "卖方库存不足") == 1
    session = SessionLocal()
    try:
        wheat = session.get(
            CompanyInventory,
            {"world_id": world_id, "company_id": "company_morning_farm", "item_id": "wheat"},
        )
        assert wheat is not None and wheat.quantity == 0
    finally:
        session.close()


def test_m16_save_restore_keeps_inventory_and_seed_idempotent(system) -> None:
    """M16：存档保持库存/预留/余额/进行中班次；恢复后重复播种无新行."""
    from app.services.save_service import SaveService

    engine, service = system
    runtime = engine.create_world("M16存档测试")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id
    save_service = SaveService(engine, SessionLocal)

    employment_id = _hire(system, world_id, "company_village_bakery", "agent_chenyu", "agent_touzi")
    view = service.list_agent_employment(world_id, "agent_chenyu")
    shift = next(s for s in view["shifts"] if s["employment_id"] == employment_id)
    advance_minutes(engine, world_id, shift["scheduled_start"] - 30 - runtime.clock.world_time)
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_chenyu"})
        assert agent is not None
        agent.location_id = "village_bakery"
        session.add(
            CompanyInventory(
                world_id=world_id, company_id="company_village_bakery",
                item_id="wheat", quantity=10,
            )
        )
        session.commit()
    finally:
        session.close()
    started = service.start_shift(world_id, shift["shift_id"], "agent_chenyu")
    assert started["status"] in {"in_progress", "late"}

    saved = save_service.save(world_id)
    runtime2 = save_service.restore(saved.save_id)
    new_world_id = runtime2.world_id
    assert new_world_id != world_id

    session = SessionLocal()
    try:
        wheat = session.get(
            CompanyInventory,
            {"world_id": new_world_id, "company_id": "company_village_bakery", "item_id": "wheat"},
        )
        assert wheat is not None
        assert wheat.quantity == 10 and wheat.reserved_quantity == 10
        bakery = session.get(
            Company, {"world_id": new_world_id, "company_id": "company_village_bakery"}
        )
        assert bakery is not None and bakery.money == 300
        shift_row = session.scalar(
            select(WorkShift).where(
                WorkShift.world_id == new_world_id,
                WorkShift.status.in_(("in_progress", "late")),
            )
        )
        assert shift_row is not None
        counts = {
            "companies": session.scalar(
                select(func.count()).select_from(Company).where(Company.world_id == new_world_id)
            ),
            "positions": session.scalar(
                select(func.count()).select_from(Position).where(Position.world_id == new_world_id)
            ),
            "jobs": session.scalar(
                select(func.count()).select_from(Job).where(Job.world_id == new_world_id)
            ),
            "locations": session.scalar(
                select(func.count()).select_from(WorldLocation).where(WorldLocation.world_id == new_world_id)
            ),
        }
    finally:
        session.close()
    # 恢复后重复 ensure_seeded → 无新行（幂等），预留保持
    service.ensure_seeded(new_world_id)
    service.ensure_seeded(new_world_id)
    session = SessionLocal()
    try:
        assert session.scalar(
            select(func.count()).select_from(Company).where(Company.world_id == new_world_id)
        ) == counts["companies"]
        assert session.scalar(
            select(func.count()).select_from(Position).where(Position.world_id == new_world_id)
        ) == counts["positions"]
        assert session.scalar(
            select(func.count()).select_from(Job).where(Job.world_id == new_world_id)
        ) == counts["jobs"]
        assert session.scalar(
            select(func.count()).select_from(WorldLocation).where(WorldLocation.world_id == new_world_id)
        ) == counts["locations"]
        wheat = session.get(
            CompanyInventory,
            {"world_id": new_world_id, "company_id": "company_village_bakery", "item_id": "wheat"},
        )
        assert wheat is not None
        assert wheat.quantity == 10 and wheat.reserved_quantity == 10
    finally:
        session.close()
