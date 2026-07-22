from __future__ import annotations

import asyncio
import logging
import signal

from day2_shared.db import make_engine, make_sessionmaker, wait_for_schema
from day2_shared.logging import configure_logging
from worker import __version__
from worker.config import Settings, get_settings
from worker.runner import run_forever, touch_heartbeat

logger = logging.getLogger(__name__)


async def amain(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level)

    engine = make_engine(settings.database_url)
    factory = make_sessionmaker(engine)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # Ready before the first poll, so the healthcheck does not flap on startup.
    touch_heartbeat(settings.heartbeat_path)
    logger.info("worker started", extra={"version": __version__})
    try:
        await wait_for_schema(
            engine, "jobs", timeout_seconds=settings.schema_wait_seconds
        )
        await run_forever(factory, settings, stop)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
