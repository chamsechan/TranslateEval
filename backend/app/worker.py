from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress, contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import os
from pathlib import Path
from sqlalchemy.engine import make_url

from .config import settings
from .database import SessionLocal, init_db
from .queue import recover_interrupted_jobs, worker_loop


@contextmanager
def exclusive_worker(database_url: str | None = None):
    """OS-owned, nonblocking single-machine lease, released even after a crash."""
    url = make_url(database_url or settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        database = Path(url.database).expanduser().resolve()
        lock_path = database.with_name(database.name + ".worker.lock")
    else:
        identity = hashlib.sha256(str(url).encode()).hexdigest()[:24]
        lock_path = settings.worker_heartbeat_file.parent / f"worker-{identity}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("此数据库已有 Worker 正在运行，请勿重复启动") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def heartbeat_loop() -> None:
    path = settings.worker_heartbeat_file
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        await asyncio.sleep(2)


async def run_worker(once: bool = False) -> None:
    with exclusive_worker():
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
