from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import api
from app.database import get_session
from app.evaluators.base import ScoreInput
from app.evaluators.registry import build_evaluator
from app.models import EvaluationTask, EvaluatorRevision, PromptProfile, PromptVersion, ScoreResult
from app.seed import seed_defaults
from test_import_and_queue import prepare_task


@pytest.fixture
def client(session_factory, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)

    def sessions():
        with session_factory() as session:
            yield session

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 4, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(api, "datetime", FixedDatetime)
    app.dependency_overrides[get_session] = sessions
    with TestClient(app) as test_client:
        yield test_client


def create_prompt(client, **fields):
    response = client.post("/api/prompt-profiles", json={"name": "测试 Prompt", **fields})
    assert response.status_code == 200, response.text
    return response.json()


def test_prompt_date_labels_increment_per_profile_and_keep_blank_templates(client):
    profile = create_prompt(client)
    first = profile["versions"][0]
    assert first["version_label"] == "20260907.1"
    assert first["system_template"] == first["user_template"] == ""
    assert first["can_delete"] and not first["has_results"]

    response = client.post(
        f"/api/prompt-profiles/{profile['id']}/versions",
        json={"system_template": "  \n", "user_template": "{translation_zh}"},
    )
    assert response.status_code == 200
    second = response.json()["versions"][0]
    assert second["version_label"] == "20260907.2"
    assert second["system_template"] == "  \n"
    assert create_prompt(client, name="另一 Prompt")["versions"][0]["version_label"] == "20260907.1"
    stored = next(item for item in client.get("/api/prompt-profiles").json() if item["id"] == profile["id"])
    assert stored["versions"][0]["version_label"] == "20260907.2"


def test_prompt_custom_labels_are_unique_and_nonempty_templates_still_validate(client):
    profile = create_prompt(client, version_label=" custom-label ")
    assert profile["versions"][0]["version_label"] == "custom-label"
    path = f"/api/prompt-profiles/{profile['id']}/versions"
    assert client.post(path, json={"version_label": "custom-label"}).status_code == 409
    assert client.post(path, json={"system_template": "{unknown}"}).status_code == 422
    assert client.post(path, json={"user_template": '{"score": 8}'}).status_code == 422
    valid = client.post(path, json={"user_template": '{translation_zh}\n{{"score": 8}}'})
    assert valid.status_code == 200
    assert valid.json()["versions"][0]["version_label"] == "20260907.1"


def test_last_prompt_version_can_be_deleted_and_recreated(client):
    profile = create_prompt(client)
    version_id = profile["versions"][0]["id"]
    assert client.delete(f"/api/prompt-versions/{version_id}").json() == {"deleted": True}
    stored = next(item for item in client.get("/api/prompt-profiles").json() if item["id"] == profile["id"])
    assert stored["versions"] == [] and stored["can_delete"]
    recreated = client.post(f"/api/prompt-profiles/{profile['id']}/versions", json={})
    assert recreated.status_code == 200
    assert len(recreated.json()["versions"]) == 1
    assert client.delete(f"/api/prompt-profiles/{profile['id']}").status_code == 200
    assert client.delete(f"/api/prompt-profiles/{profile['id']}").status_code == 404
    assert client.delete(f"/api/prompt-versions/{version_id}").status_code == 404


def test_deleted_default_prompt_stays_deleted_after_startup_and_can_be_recreated(client, session_factory):
    profile = client.get("/api/prompt-profiles").json()[0]
    assert client.delete(f"/api/prompt-profiles/{profile['id']}").status_code == 200
    with session_factory() as session:
        seed_defaults(session)
        stored = session.get(PromptProfile, profile["id"])
        assert stored.deleted and not stored.versions
    assert client.get("/api/prompt-profiles").json() == []
    assert client.post(f"/api/prompt-profiles/{profile['id']}/versions", json={}).status_code == 404
    recreated = create_prompt(client, name=profile["name"])
    assert recreated["id"] == profile["id"]
    assert len(recreated["versions"]) == 1


def add_score(session, prompt_version_id):
    revision = session.scalar(sa.select(EvaluatorRevision))
    score = ScoreResult(
        evaluator_type="openai_compatible_llm",
        evaluator_revision_id=revision.id,
        prompt_version_id=prompt_version_id,
        source_language="de",
        source_hash="source",
        reference_hash="reference",
        translation_hash="translation",
        score=9,
        score_min=0,
        score_max=10,
        unit="point",
    )
    session.add(score)
    session.commit()
    return score


@pytest.mark.parametrize("reference", ["cached_result", "task", "task_with_cached_result"])
def test_referenced_prompt_cannot_be_deleted(client, session_factory, reference):
    profile = create_prompt(client)
    version_id = profile["versions"][0]["id"]
    if reference == "cached_result":
        with session_factory() as session:
            add_score(session, version_id)
    else:
        task_id = prepare_task(session_factory)
        with session_factory() as session:
            task = session.get(EvaluationTask, task_id)
            job = task.dataset_jobs[0].evaluator_jobs[0]
            job.prompt_version_id = version_id
            if reference == "task_with_cached_result":
                # A cache hit can point at a result originally produced by another Prompt.
                score = add_score(session, None)
                job.items[0].score_result_id = score.id
            session.commit()
    stored = next(item for item in client.get("/api/prompt-profiles").json() if item["id"] == profile["id"])
    expected_results = reference != "task"
    for item in [stored, stored["versions"][0]]:
        assert item["has_results"] is expected_results
        assert not item["can_delete"]
        assert item["delete_block_reason"]
    for path in [f"/api/prompt-versions/{version_id}", f"/api/prompt-profiles/{profile['id']}"]:
        response = client.delete(path)
        assert response.status_code == 409
    with session_factory() as session:
        assert session.get(PromptVersion, version_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("system", "user", "expected"),
    [
        ("", "{translation_zh}", [{"role": "user", "content": "你好"}]),
        ("Score", " \n", [{"role": "system", "content": "Score"}]),
        ("", "", []),
        (" \t", "\n", []),
    ],
)
async def test_blank_prompt_roles_are_absent_from_actual_llm_messages(system, user, expected):
    evaluator = build_evaluator(
        "openai_compatible_llm",
        {"base_url": "https://judge.invalid/v1", "model": "judge", "api_key": "test"},
        system_template=system,
        user_template=user,
    )

    async def handler(request):
        assert json.loads(request.content)["messages"] == expected
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"score": 9, "reason": "准确"}'}}]}
        )

    await evaluator.client.aclose()
    evaluator.client = httpx.AsyncClient(
        base_url="https://judge.invalid/v1/", transport=httpx.MockTransport(handler)
    )
    try:
        result = await evaluator.evaluate_one(ScoreInput("de", "Hallo", "你好", "你好"))
        assert result.score == 9
    finally:
        await evaluator.close()


def test_prompt_label_migration_preserves_existing_versions(tmp_path):
    migration_path = Path(__file__).resolve().parents[1] / "alembic/versions/0005_prompt_version_labels.py"
    spec = importlib.util.spec_from_file_location("prompt_label_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    metadata = sa.MetaData()
    sa.Table("prompt_profiles", metadata, sa.Column("id", sa.String(36), primary_key=True))
    versions = sa.Table(
        "prompt_versions", metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("profile_id", "version", name="uq_prompt_version"),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(versions.insert(), [
            {"id": "v1", "profile_id": "p1", "version": 1, "created_at": datetime(2026, 9, 6, 16)},
            {"id": "v2", "profile_id": "p1", "version": 2, "created_at": datetime(2026, 9, 7, 4)},
            {"id": "v3", "profile_id": "p1", "version": 3, "created_at": datetime(2026, 9, 7, 16)},
            {"id": "v4", "profile_id": "p2", "version": 1, "created_at": datetime(2026, 9, 7, 4)},
        ])
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        labels = dict(connection.execute(sa.text("SELECT id, version_label FROM prompt_versions")).all())
        assert labels == {"v1": "20260907.1", "v2": "20260907.2", "v3": "20260908.1", "v4": "20260907.1"}
        migration.upgrade()  # Current metadata/fresh-install guard.
        assert not next(c for c in sa.inspect(connection).get_columns("prompt_versions") if c["name"] == "version_label")["nullable"]
        migration.downgrade()
        assert "version_label" not in {c["name"] for c in sa.inspect(connection).get_columns("prompt_versions")}
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM prompt_versions")) == 4
    engine.dispose()
