"""apply_job / withdraw_job_application / review_job_application tools (M13).

Applicant + manager tools funnel through CompanyEmploymentService — the
company rule gate (R23/R24/R25: vacancies, uniqueness, manager permission).
They never touch SQL/ORM/WS directly and return the same structured JSON the
decision service records in ``llm_runs.tool_result``:
``{"success", "reason", "event"}``.
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from app.agents.context import AgentToolContext
from app.services.company_employment_service import CompanyEmploymentError


def _ok(payload: dict | None) -> str:
    return json.dumps({"success": True, "reason": None, "event": payload}, ensure_ascii=False)


def _err(reason: str) -> str:
    return json.dumps({"success": False, "reason": reason, "event": None}, ensure_ascii=False)


def _service(ctx: RunContextWrapper[AgentToolContext]):
    return getattr(ctx.context.engine, "company_employment_service", None)


@function_tool
async def apply_job(
        ctx: RunContextWrapper[AgentToolContext],
        opening_id: str,
        reason: str,
) -> str:
    """申请【公开招聘】中的正式职位（opening_id 必须是公告里括号内的完整 id）。
    录用与否由企业经理决定；重复申请、岗位已满或招聘关闭会被拒绝。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    try:
        payload = service.apply(
            world_id=ctx.context.world_id,
            opening_id=opening_id,
            agent_id=ctx.context.agent_id,
            reason=reason,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def withdraw_job_application(
        ctx: RunContextWrapper[AgentToolContext],
        application_id: str,
        reason: str,
) -> str:
    """撤回自己尚未被处理的求职申请（application_id 必须是【我的申请】里括号内的完整 id）。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    try:
        payload = service.withdraw(
            world_id=ctx.context.world_id,
            application_id=application_id,
            agent_id=ctx.context.agent_id,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def review_job_application(
        ctx: RunContextWrapper[AgentToolContext],
        application_id: str,
        decision: str,
        reason: str,
) -> str:
    """审核【待审核求职申请】里的申请（仅企业经理可用）。
    decision 只能是 accept（录用，立即建立合同并安排班次）或 reject（拒绝）。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    if decision not in {"accept", "reject"}:
        return _err("decision 必须是 accept 或 reject")
    try:
        payload = service.review(
            world_id=ctx.context.world_id,
            application_id=application_id,
            manager_agent_id=ctx.context.agent_id,
            decision=decision,
            reason=reason,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def start_shift(
        ctx: RunContextWrapper[AgentToolContext],
        shift_id: str,
        reason: str,
) -> str:
    """在【今天班次】的工作地点签到开始正式班次（shift_id 用班次行的完整 id）。
    必须位于企业地点；可提前 30 分钟签到，迟到上限 120 分钟，超时班次作缺勤。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    try:
        payload = service.start_shift(
            world_id=ctx.context.world_id,
            shift_id=shift_id,
            agent_id=ctx.context.agent_id,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def resign_job(
        ctx: RunContextWrapper[AgentToolContext],
        employment_id: str,
        reason: str,
) -> str:
    """辞去当前正式工作（employment_id 用【正式职业】行的完整 id）。
    辞职后未来班次取消、岗位重新开放；欠薪仍然保留。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    try:
        payload = service.resign(
            world_id=ctx.context.world_id,
            employment_id=employment_id,
            agent_id=ctx.context.agent_id,
            reason=reason,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def request_leave(
        ctx: RunContextWrapper[AgentToolContext],
        shift_id: str,
        reason: str,
) -> str:
    """为【今天班次】申请请假（shift_id 用班次行的完整 id）。
    由企业经理审批；批准后该班次不判缺勤也不发工资。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    try:
        payload = service.request_leave(
            world_id=ctx.context.world_id,
            shift_id=shift_id,
            agent_id=ctx.context.agent_id,
            reason=reason,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def review_leave_request(
        ctx: RunContextWrapper[AgentToolContext],
        request_id: str,
        decision: str,
        reason: str,
) -> str:
    """审批员工的请假申请（仅企业经理可用；request_id 用【待审批请假】行的完整 id）。
    decision 只能是 approve（准假，班次转请假）或 reject（拒绝，班次保持待签到）。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    if decision not in {"approve", "reject"}:
        return _err("decision 必须是 approve 或 reject")
    try:
        payload = service.review_leave_request(
            world_id=ctx.context.world_id,
            request_id=request_id,
            manager_agent_id=ctx.context.agent_id,
            decision=decision,
            reason=reason,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def terminate_employment(
        ctx: RunContextWrapper[AgentToolContext],
        employment_id: str,
        reason: str,
) -> str:
    """解雇本企业的员工（仅企业经理可用；employment_id 用员工列表里的完整 id）。
    合同终止、未来班次取消、招聘名额恢复；欠薪不会因解雇消失。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    try:
        payload = service.terminate(
            world_id=ctx.context.world_id,
            employment_id=employment_id,
            manager_agent_id=ctx.context.agent_id,
            reason=reason,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def pause_recruitment(
        ctx: RunContextWrapper[AgentToolContext],
        position_id: str,
        reason: str,
) -> str:
    """暂停某个岗位的招聘（仅企业经理可用；position_id 用【企业经营】行里的完整 id）。
    暂停期间该岗位不再接受新申请，已有员工不受影响。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    try:
        payload = service.pause_recruitment(
            world_id=ctx.context.world_id,
            position_id=position_id,
            manager_agent_id=ctx.context.agent_id,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def resume_recruitment(
        ctx: RunContextWrapper[AgentToolContext],
        position_id: str,
        reason: str,
) -> str:
    """恢复某个岗位的招聘（仅企业经理可用；岗位需处于暂停状态且企业正常经营）。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    try:
        payload = service.resume_recruitment(
            world_id=ctx.context.world_id,
            position_id=position_id,
            manager_agent_id=ctx.context.agent_id,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def purchase_company_goods(
        ctx: RunContextWrapper[AgentToolContext],
        buyer_company_id: str,
        seller_company_id: str,
        item_id: str,
        reason: str,
        quantity: int = 1,
) -> str:
    """按服务器固定价从其他企业采购原料（仅本企业经理可用）。
    价格、可采购数量上限与货源由世界规则决定，不接受自定义价格；
    buyer_company_id/seller_company_id/item_id 用【企业经营】采购行的完整 id。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    quantity = max(1, min(int(quantity), 99))
    try:
        payload = service.purchase_company_goods(
            world_id=ctx.context.world_id,
            buyer_company_id=buyer_company_id,
            seller_company_id=seller_company_id,
            manager_agent_id=ctx.context.agent_id,
            item_id=item_id,
            quantity=quantity,
            reason=reason,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))


@function_tool
async def stock_store(
        ctx: RunContextWrapper[AgentToolContext],
        company_id: str,
        store_id: str,
        item_id: str,
        reason: str,
        quantity: int = 1,
) -> str:
    """把本企业仓库的货物上架到自有商店货架（仅企业经理可用）。
    上架不产生资金流动，货架容量不足会被拒绝；store_id/item_id 用
    【企业经营】上架行的完整 id。"""
    service = _service(ctx)
    if service is None:
        return _err("企业服务未初始化")
    quantity = max(1, min(int(quantity), 99))
    try:
        payload = service.stock_store(
            world_id=ctx.context.world_id,
            company_id=company_id,
            store_id=store_id,
            manager_agent_id=ctx.context.agent_id,
            item_id=item_id,
            quantity=quantity,
            reason=reason,
        )
        return _ok(payload)
    except CompanyEmploymentError as exc:
        return _err(str(exc))
