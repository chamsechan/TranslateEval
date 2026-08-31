from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import UTC, datetime

from .config import settings
from .database import SessionLocal, init_db
from .queue import recover_interrupted_jobs, worker_loop


async def heartbeat_loop() -> None:
    path = settings.worker_heartbeat_file
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        await asyncio.sleep(2)


async def run_worker(once: bool = False) -> None:
    with SessionLocal() as session:
        recover_interrupted_jobs(session)

    heartbeat = asyncio.create_task(heartbeat_loop())
    try:
        await worker_loop(once=once)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        with suppress(FileNotFoundError):
            settings.worker_heartbeat_file.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Translation evaluation worker")
    parser.add_argument("--once", action="store_true", help="Process at most one evaluator job")
    args = parser.parse_args()
    init_db()
    asyncio.run(run_worker(once=args.once))


if __name__ == "__main__":
    main()
