"""AI Tiny World backend — FastAPI application entry point."""

import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config.settings import get_settings
from app.services.world_config_loader import WorldConfigError, load_world_config

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
    """Startup: configure logging and preload world config; fail fast if map is missing."""
    _configure_logging(settings.log_level)
    try:
        world = load_world_config(settings)
    except WorldConfigError as exc:
        logger.critical("Failed to load world config: {}", exc)
        raise RuntimeError(f"World data unavailable: {exc}") from exc
    app.state.world_config = world
    logger.info("{} ready (map v{})", settings.app_name, world.map_version)
    yield


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
        content={"detail": exc.errors(), "request_id": request_id},
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


# World data served verbatim for the frontend (maps, tilesets, images).
app.mount(
    "/assets/world_data",
    StaticFiles(directory=str(settings.world_data_dir), check_dir=False, html=False),
    name="world_data",
)
