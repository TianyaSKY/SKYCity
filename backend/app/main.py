"""AI Tiny World backend — FastAPI application entry point."""

import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.saves import router as saves_router
from app.api.websocket import router as websocket_router
from app.api.worlds import router as worlds_router
from app.config.settings import get_settings
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.build_service import BuildService
from app.services.conversation_service import ConversationService
from app.services.crop_service import CropService
from app.services.economy_service import EconomyService
from app.services.god_action_service import GodActionService
from app.services.save_service import SaveService
from app.services.stock_service import StockService
from app.services.transfer_service import TransferService
from app.services.world_config_loader import WorldConfigError, load_world_config
from app.world_engine.engine import WorldEngine

settings = get_settings()


def _configure_logging(level: str) -> None:
    logger.remove()
    logger.add(
        lambda message: print(message, end=""),
        level=level.upper(),
        enqueue=False,
        colorize=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: configure logging, preload world config, start the world engine.

    The engine owns the game clock, the event bus, and WebSocket delivery; it
    is the single source of truth for world state (see docs/architecture.md).
    """
    _configure_logging(settings.log_level)
    try:
        world = load_world_config(settings)
    except WorldConfigError as exc:
        logger.critical("Failed to load world config: {}", exc)
        raise RuntimeError(f"World data unavailable: {exc}") from exc
    app.state.world_config = world

    engine = WorldEngine(
        session_factory=SessionLocal,
        world_config=world,
        world_data_dir=settings.world_data_dir,
    )
    service = ActionExecutionService(engine, SessionLocal)
    engine.action_service = service
    economy_service = EconomyService(engine, SessionLocal)
    engine.economy_service = economy_service
    decision_service = DecisionService(engine, SessionLocal)
    engine.decision_service = decision_service
    conversation_service = ConversationService(engine, SessionLocal)
    engine.conversation_service = conversation_service
    god_service = GodActionService(engine, SessionLocal)
    engine.god_action_service = god_service
    stock_service = StockService(engine, SessionLocal)
    engine.stock_service = stock_service
    transfer_service = TransferService(engine, SessionLocal)
    engine.transfer_service = transfer_service
    build_service = BuildService(engine, SessionLocal)
    engine.build_service = build_service
    crop_service = CropService(engine, SessionLocal)
    engine.crop_service = crop_service
    save_service = SaveService(engine, SessionLocal)
    engine.save_service = save_service
    app.state.engine = engine
    app.state.action_service = service
    app.state.economy_service = economy_service
    app.state.decision_service = decision_service
    app.state.conversation_service = conversation_service
    app.state.god_action_service = god_service
    app.state.stock_service = stock_service
    app.state.transfer_service = transfer_service
    app.state.build_service = build_service
    app.state.crop_service = crop_service
    app.state.save_service = save_service

    await engine.start()
    engine.load_existing()
    logger.info("{} ready (map v{})", settings.app_name, world.map_version)
    yield
    await engine.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    request_id = uuid.uuid4().hex
    logger.warning("Request {} validation error: {}", request_id, exc.errors())
    return JSONResponse(
        status_code=422,
        content={
            "detail": jsonable_encoder(exc.errors()),
            "request_id": request_id,
        },
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    request_id = uuid.uuid4().hex
    logger.exception("Request {} failed: {}", request_id, exc)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal Server Error", "request_id": request_id},
    )


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe; reports the loaded world map version."""
    return {
        "status": "ok",
        "map_version": app.state.world_config.map_version,
    }


app.include_router(worlds_router)
app.include_router(websocket_router)
app.include_router(saves_router)

# World data served verbatim for the frontend (maps, tilesets, images).
app.mount(
    "/assets/world_data",
    StaticFiles(directory=str(settings.world_data_dir), check_dir=False, html=False),
    name="world_data",
)
