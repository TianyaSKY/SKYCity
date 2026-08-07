"""World REST API: create/list/get worlds, control clock, run manual actions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.database.models.llm_runs import LLMRun
from app.database.models.worlds import World
from app.database.session import SessionLocal
from app.schemas.actions import ActionRequest, ActionSuccess
from app.schemas.god_actions import GodActionRequest, GodActionResponse
from app.schemas.locations import LocationDetail
from app.schemas.saves import RestoreRequest, RestoreResponse, SaveResponse
from app.schemas.snapshots import (
    AutonomousRequest,
    CreateWorldRequest,
    CreateWorldResponse,
    OkResponse,
    SpeedRequest,
    WorldInfo,
)
from app.schemas.stocks import StocksResponse
from app.world_engine.engine import WorldEngine

router = APIRouter(prefix="/api/worlds", tags=["worlds"])


def _engine(request: Request) -> WorldEngine:
    return request.app.state.engine


@router.post("", response_model=CreateWorldResponse, status_code=201)
async def create_world(request: Request, body: CreateWorldRequest | None = None) -> CreateWorldResponse:
    """Create a new world: agents seeded from identity cards, locations from the map."""
    runtime = _engine(request).create_world(
        body.name if body else None,
        autonomous=body.autonomous if body else False,
    )
    # B3: seeding moved out of the read paths — new worlds are seeded here,
    # resumed runtimes at startup (main.lifespan) and on restore
    # (save_service), so reads stay side-effect free.
    request.app.state.company_employment_service.ensure_seeded(runtime.world_id)
    return CreateWorldResponse(
        world_id=runtime.world_id,
        world_time=runtime.clock.world_time,
        speed=runtime.clock.speed,
        paused=runtime.clock.paused,
        autonomous=bool(body.autonomous if body else False),
    )


@router.get("", response_model=list[WorldInfo])
async def list_worlds(request: Request) -> list[WorldInfo]:
    """All worlds, ordered by id."""
    session = SessionLocal()
    try:
        worlds = session.scalars(select(World).order_by(World.world_id)).all()
        return [
            WorldInfo(
                world_id=w.world_id,
                name=w.name,
                world_time=w.world_time,
                speed=w.speed,
                paused=w.paused,
                autonomous=w.autonomous,
            )
            for w in worlds
        ]
    finally:
        session.close()


@router.delete("/{world_id}", response_model=OkResponse)
async def delete_world(request: Request, world_id: str) -> OkResponse:
    """Permanently delete a world (agents, events, llm_runs, saves cascade)."""
    if not _engine(request).delete_world(world_id):
        raise HTTPException(status_code=404, detail="世界不存在")
    return OkResponse(ok=True)


@router.get("/{world_id}", response_model=WorldInfo)
async def get_world(request: Request, world_id: str) -> WorldInfo:
    """One world's info (404 when missing)."""
    session = SessionLocal()
    try:
        world = session.get(World, world_id)
    finally:
        session.close()
    if world is None:
        raise HTTPException(status_code=404, detail="世界不存在")
    return WorldInfo(
        world_id=world.world_id,
        name=world.name,
        world_time=world.world_time,
        speed=world.speed,
        paused=world.paused,
        autonomous=world.autonomous,
    )


@router.get("/{world_id}/snapshot")
async def world_snapshot(request: Request, world_id: str) -> dict:
    """Full snapshot payload (same shape as the WS world_snapshot envelope)."""
    payload = _engine(request).snapshot(world_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="世界不存在")
    return payload


@router.post("/{world_id}/pause", response_model=OkResponse)
async def pause_world(request: Request, world_id: str) -> OkResponse:
    """Freeze the clock + scheduler (idempotent)."""
    engine = _engine(request)
    found, envelope = engine.set_paused(world_id, True)
    if not found:
        raise HTTPException(status_code=404, detail="世界不存在")
    if envelope is not None:
        await engine.flush_pending_now(world_id)
    return OkResponse()


@router.post("/{world_id}/resume", response_model=OkResponse)
async def resume_world(request: Request, world_id: str) -> OkResponse:
    """Unfreeze the clock + scheduler (idempotent)."""
    engine = _engine(request)
    found, envelope = engine.set_paused(world_id, False)
    if not found:
        raise HTTPException(status_code=404, detail="世界不存在")
    if envelope is not None:
        await engine.flush_pending_now(world_id)
    return OkResponse()


@router.post("/{world_id}/speed", response_model=OkResponse)
async def set_speed(request: Request, world_id: str, body: SpeedRequest) -> OkResponse:
    """Change clock speed; only 1/2/5/10 are valid (422 otherwise)."""
    engine = _engine(request)
    found, envelope = engine.set_speed(world_id, body.speed)
    if not found:
        raise HTTPException(status_code=404, detail="世界不存在")
    if envelope is not None:
        await engine.flush_pending_now(world_id)
    return OkResponse()


@router.post("/{world_id}/autonomous", response_model=OkResponse)
async def set_autonomous(
        request: Request, world_id: str, body: AutonomousRequest
) -> OkResponse:
    """Enable/disable the LLM decision loop; enabling arms initial decisions."""
    engine = _engine(request)
    found, changed = engine.set_autonomous(world_id, body.enabled)
    if not found:
        raise HTTPException(status_code=404, detail="世界不存在")
    if changed:
        await engine.flush_pending_now(world_id)
    return OkResponse()


@router.get("/{world_id}/agents/{agent_id}/decisions")
async def list_decisions(
        request: Request, world_id: str, agent_id: str, limit: int = 50
) -> list[dict]:
    """LLM decision history for one agent (recent first), from llm_runs."""
    if _engine(request).get_runtime(world_id) is None:
        raise HTTPException(status_code=404, detail="世界不存在")
    session = SessionLocal()
    try:
        rows = session.scalars(
            select(LLMRun)
            .where(LLMRun.world_id == world_id, LLMRun.agent_id == agent_id)
            .order_by(LLMRun.created_at.desc(), LLMRun.run_id.desc())
            .limit(min(max(limit, 1), 500))
        ).all()
        return [row.to_dict() for row in rows]
    finally:
        session.close()


@router.get("/{world_id}/agents/{agent_id}/conversations")
async def list_conversations(
        request: Request, world_id: str, agent_id: str, limit: int = 20
) -> list[dict]:
    """Conversation history for one agent (newest first), each with messages."""
    engine = _engine(request)
    if engine.get_runtime(world_id) is None:
        raise HTTPException(status_code=404, detail="世界不存在")
    return engine.conversation_service.history(world_id, agent_id, limit)


@router.get("/{world_id}/agents/{agent_id}/memories")
async def list_memories(
        request: Request, world_id: str, agent_id: str, limit: int = 30
) -> list[dict]:
    """M6: memories for one agent, newest first (working|episodic|semantic)."""
    engine = _engine(request)
    if engine.get_runtime(world_id) is None:
        raise HTTPException(status_code=404, detail="世界不存在")
    return engine.memory_service.list_memories(world_id, agent_id, limit)


@router.get("/{world_id}/agents/{agent_id}/relationships")
async def list_relationships(
        request: Request, world_id: str, agent_id: str
) -> list[dict]:
    """M6: directional relationships where the agent is the source."""
    engine = _engine(request)
    if engine.get_runtime(world_id) is None:
        raise HTTPException(status_code=404, detail="世界不存在")
    return engine.relationship_service.list_for_agent(world_id, agent_id)


@router.post("/{world_id}/agents/{agent_id}/actions", response_model=None)
async def agent_action(
        request: Request, world_id: str, agent_id: str, body: ActionRequest
) -> JSONResponse:
    """Manual action endpoint (M2): move or wait, validated by world rules."""
    engine = _engine(request)
    service = engine.action_service
    ok, envelope, reason = service.execute_action(world_id, agent_id, body)
    if not ok:
        return JSONResponse(
            status_code=409, content={"success": False, "reason": reason}
        )
    await engine.flush_pending_now(world_id)
    return JSONResponse(
        content=ActionSuccess(success=True, event=envelope).model_dump()
    )


@router.get("/{world_id}/agents/{agent_id}")
async def get_agent(request: Request, world_id: str, agent_id: str) -> dict:
    """M7: one agent's detail — identity card, state, inventory, action."""
    detail = _engine(request).agent_detail(world_id, agent_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return detail


@router.get("/{world_id}/locations/{location_id}", response_model=LocationDetail)
async def get_location_detail(
        request: Request, world_id: str, location_id: str
) -> LocationDetail:
    """One location's detail: base fields + occupants + store products + jobs."""
    detail = _engine(request).location_detail(world_id, location_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="地点不存在")
    return LocationDetail(**detail)


@router.post("/{world_id}/god-actions", response_model=GodActionResponse)
async def god_action(
        request: Request, world_id: str, body: GodActionRequest
) -> GodActionResponse:
    """M7: apply one god intervention — audit row + events + WS push."""
    engine = _engine(request)
    service = engine.god_action_service
    if service is None:
        raise HTTPException(status_code=503, detail="神谕服务未就绪")
    result = service.apply(
        world_id, body.command_type, body.target_id, body.parameters, body.reason
    )
    await engine.flush_pending_now(world_id)
    return GodActionResponse(**result)


@router.post("/restore", response_model=RestoreResponse, status_code=201)
async def restore_world(request: Request, body: RestoreRequest) -> RestoreResponse:
    """M9: rebuild a NEW world from a save and start it running."""
    runtime = _engine(request).save_service.restore(body.save_id)
    session = SessionLocal()
    try:
        world = session.get(World, runtime.world_id)
    finally:
        session.close()
    if world is None:  # pragma: no cover - restore just created the row
        raise HTTPException(status_code=404, detail="世界不存在")
    return RestoreResponse(
        world_id=world.world_id,
        save_id=body.save_id,
        world_time=world.world_time,
        speed=world.speed,
        paused=world.paused,
        autonomous=world.autonomous,
    )


@router.post("/{world_id}/save", response_model=SaveResponse, status_code=201)
async def save_world(request: Request, world_id: str) -> SaveResponse:
    """M9: serialize the world's full state into a save row."""
    result = _engine(request).save_service.save(world_id)
    return SaveResponse(
        save_id=result.save_id,
        world_id=result.world_id,
        created_at=result.created_at,
    )


@router.get("/{world_id}/replay")
async def replay_world(request: Request, world_id: str) -> dict:
    """M9: initial snapshot + every event envelope, sequence ascending."""
    return _engine(request).save_service.replay(world_id)


@router.get("/{world_id}/events")
async def list_events(request: Request, world_id: str, after_sequence: int = 0) -> list[dict]:
    """Envelopes with sequence > after_sequence (WS gap recovery)."""
    if _engine(request).get_runtime(world_id) is None:
        raise HTTPException(status_code=404, detail="世界不存在")
    envelopes = _engine(request).events_after(world_id, after_sequence)
    return [envelope.model_dump() for envelope in envelopes]


@router.get("/{world_id}/stocks", response_model=StocksResponse)
async def world_stocks(request: Request, world_id: str) -> StocksResponse:
    """M10: 全部股票行情 + 全量持仓(WS 事件增量维护前端状态)。"""
    result = _engine(request).stock_service.list_stocks(world_id)
    if result is None:
        raise HTTPException(status_code=404, detail="世界不存在")
    return StocksResponse(**result)
