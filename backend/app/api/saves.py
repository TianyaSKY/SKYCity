"""Save listing API (M9): GET /api/saves?world_id=... newest first."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.schemas.saves import SaveInfo
from app.services.save_service import SaveService

router = APIRouter(prefix="/api/saves", tags=["saves"])


def _save_service(request: Request) -> SaveService:
    return request.app.state.save_service


@router.get("", response_model=list[SaveInfo])
async def list_saves(
    request: Request, world_id: str | None = None
) -> list[SaveInfo]:
    """All saves, newest first; pass world_id to filter one world."""
    return [SaveInfo(**item) for item in _save_service(request).list_saves(world_id)]
