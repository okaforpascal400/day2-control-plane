from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import __version__
from api.config import Settings, get_settings
from api.routes import health, items, jobs
from day2_shared.db import create_schema, make_engine, make_sessionmaker
from day2_shared.logging import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = make_engine(settings.database_url)
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    if settings.create_schema_on_start:
        await create_schema(engine)
        logger.info("schema ready")

    logger.info("api started", extra={"version": __version__})
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("api stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level)

    app = FastAPI(
        title="day2-control-plane api",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(health.router)
    app.include_router(items.router)
    app.include_router(jobs.router)
    return app


app = create_app()
