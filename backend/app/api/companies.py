"""Company, recruitment and formal-employment REST endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.schemas.companies import (
    EmploymentResignRequest,
    JobApplicationRequest,
    JobApplicationReviewRequest,
    ShiftStartRequest,
)
from app.services.company_employment_service import (
    CompanyEmploymentError,
    CompanyEmploymentService,
)

router = APIRouter(prefix="/api/worlds", tags=["companies"])


def _service(request: Request) -> CompanyEmploymentService:
    return request.app.state.company_employment_service


def _translate(exc: CompanyEmploymentError) -> HTTPException:
    detail = str(exc)
    status = 404 if detail in {"世界不存在", "申请不存在", "班次不存在", "劳动合同不存在"} else 409
    return HTTPException(status_code=status, detail=detail)


@router.get("/{world_id}/companies")
async def list_companies(request: Request, world_id: str) -> list[dict]:
    try:
        return _service(request).list_companies(world_id)
    except CompanyEmploymentError as exc:
        raise _translate(exc) from exc


@router.get("/{world_id}/job-openings")
async def list_job_openings(request: Request, world_id: str) -> list[dict]:
    try:
        return _service(request).list_openings(world_id)
    except CompanyEmploymentError as exc:
        raise _translate(exc) from exc


@router.post("/{world_id}/job-openings/{opening_id}/apply", status_code=201)
async def apply_for_job(
    request: Request,
    world_id: str,
    opening_id: str,
    body: JobApplicationRequest,
) -> dict:
    try:
        result = _service(request).apply(world_id, opening_id, body.agent_id, body.reason)
        await request.app.state.engine.flush_pending_now(world_id)
        return result
    except CompanyEmploymentError as exc:
        raise _translate(exc) from exc


@router.post("/{world_id}/job-applications/{application_id}/review")
async def review_job_application(
    request: Request,
    world_id: str,
    application_id: str,
    body: JobApplicationReviewRequest,
) -> dict:
    try:
        result = _service(request).review(
            world_id,
            application_id,
            body.manager_agent_id,
            body.decision,
            body.reason,
        )
        await request.app.state.engine.flush_pending_now(world_id)
        return result
    except CompanyEmploymentError as exc:
        raise _translate(exc) from exc


@router.get("/{world_id}/agents/{agent_id}/employment")
async def get_agent_employment(request: Request, world_id: str, agent_id: str) -> dict:
    return _service(request).list_agent_employment(world_id, agent_id)


@router.post("/{world_id}/work-shifts/{shift_id}/start")
async def start_work_shift(
    request: Request,
    world_id: str,
    shift_id: str,
    body: ShiftStartRequest,
) -> dict:
    try:
        result = _service(request).start_shift(world_id, shift_id, body.agent_id)
        await request.app.state.engine.flush_pending_now(world_id)
        return result
    except CompanyEmploymentError as exc:
        raise _translate(exc) from exc


@router.post("/{world_id}/employments/{employment_id}/resign")
async def resign_employment(
    request: Request,
    world_id: str,
    employment_id: str,
    body: EmploymentResignRequest,
) -> dict:
    try:
        result = _service(request).resign(world_id, employment_id, body.agent_id, body.reason)
        await request.app.state.engine.flush_pending_now(world_id)
        return result
    except CompanyEmploymentError as exc:
        raise _translate(exc) from exc
