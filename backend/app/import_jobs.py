"""Durable import requests with single-host ownership and atomic publication.

The API records requests quickly. A separate thread owns the database's import
lock and replays interrupted requests after restart. The underlying report
claim is idempotent, including a crash after publication but before completion
was recorded here. Partial datasets never become visible.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import logging
from pathlib import Path
import threading

from fastapi import HTTPException
from sqlalchemy import select, update

from .config import settings
from .importers import commit_dataset_import
from .models import ImportCommitJob
from .schemas import CommitSubmissionRequest

logger = logging.getLogger(__name__)


@contextmanager
def import_dispatch_lock(engine):
    url = engine.url
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        database = Path(url.database).expanduser().resolve()
        path = database.with_name(database.name + ".imports.lock")
    else:
        identity = hashlib.sha256(str(url).encode()).hexdigest()[:24]
        path = settings.import_dir / f"dispatcher-{identity}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def recover_import_jobs(factory) -> int:
    """Only the dispatcher holding the OS lock may recover running imports."""
    with factory() as session:
        result = session.execute(update(ImportCommitJob).where(
            ImportCommitJob.status == "running",
        ).values(status="queued", phase="queued", started_at=None, finished_at=None))
        session.commit()
        return result.rowcount


def process_import_job(factory, job_id: str) -> bool:
    with factory() as session:
        claimed = session.execute(update(ImportCommitJob).where(
            ImportCommitJob.id == job_id, ImportCommitJob.status == "queued",
        ).values(status="running", phase="validating", started_at=datetime.now(UTC), error=None))
        if claimed.rowcount != 1:
            session.rollback()
            return False
        job = session.get(ImportCommitJob, job_id)
        report_id, kind, request = job.report_id, job.kind, job.request
        session.commit()

    def writing():
        # Called after indexing, immediately before the publication write lock.
        with factory() as progress:
            progress.execute(update(ImportCommitJob).where(ImportCommitJob.id == job_id).values(phase="writing"))
            progress.commit()

    try:
        with factory() as session:
            session.info["import_writing"] = writing
            if kind == "dataset":
                version = commit_dataset_import(session, report_id)
                result = {"dataset_version_id": version.id, "content_sha256": version.content_sha256}
            else:
                from .api import commit_submission
                result = commit_submission(report_id, CommitSubmissionRequest.model_validate(request), session)
        with factory() as session:
            session.execute(update(ImportCommitJob).where(ImportCommitJob.id == job_id).values(
                status="completed", phase="completed", result=result, error=None, finished_at=datetime.now(UTC),
            ))
            session.commit()
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        with factory() as session:
            session.execute(update(ImportCommitJob).where(ImportCommitJob.id == job_id).values(
                status="failed", phase="failed", error=str(detail), finished_at=datetime.now(UTC),
            ))
            session.commit()
    return True


class ImportDispatcher:
    def __init__(self, factory):
        self.factory = factory
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name="translateeval-imports", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stopping.set()
        # An in-progress publication may finish atomically during shutdown. If
        # the process is killed sooner, its durable request is replayed on boot.
        self.thread.join(timeout=5)

    def run(self):
        while not self.stopping.is_set():
            try:
                with self.factory() as session:
                    engine = session.get_bind()
                with import_dispatch_lock(engine) as owned:
                    if owned:
                        recover_import_jobs(self.factory)
                        self.drain()
            except Exception:
                logger.exception("Import dispatcher will retry; requests remain recoverable")
            self.stopping.wait(1)

    def drain(self):
        while not self.stopping.is_set():
            with self.factory() as session:
                job_id = session.scalar(select(ImportCommitJob.id).where(
                    ImportCommitJob.status == "queued",
                ).order_by(ImportCommitJob.created_at, ImportCommitJob.id).limit(1))
            if job_id:
                process_import_job(self.factory, job_id)
            else:
                self.stopping.wait(.5)
