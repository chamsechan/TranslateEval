from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import event, select

from app.models import Dataset, DatasetSample, DatasetVersion, EvaluationItem, EvaluatorJob, EvaluatorRevision, Language, Prediction
from test_audit_regressions import client  # isolated API fixture
from test_result_inspection import mixed_results


@pytest.fixture
def version_history(session_factory, mixed_results):
    job_id, _ = mixed_results
    with session_factory() as session:
        old = session.get(EvaluatorJob, job_id).dataset_job.submission_dataset.dataset_version
        for code, name in [("de", "德语"), ("th", "泰语"), ("vi", "越南语")]:
            session.get(Language, code).name_zh = name
        latest = DatasetVersion(
            dataset_id=old.dataset_id, version_label="navigation-new-version", content_sha256="a" * 64,
            sample_count=2, source_languages=["th"], created_at=old.created_at + timedelta(days=1),
            change_note="Different language composition",
        )
        session.add(latest)
        session.flush()
        for number in range(2):
            session.add(DatasetSample(
                dataset_version_id=latest.id, sample_id=f"new-{number}", source_language="th",
                source_text=f"New sample {number}", reference_zh="新译文", source_hash="s",
                reference_hash="r", content_hash=f"new-{number}",
            ))
        session.commit()
        return old.dataset_id, old.id, latest.id


def test_version_navigation_keeps_historical_language_counts(client, version_history):
    dataset_id, old_id, latest_id = version_history
    dataset = next(row for row in client.get("/api/datasets").json() if row["id"] == dataset_id)
    versions = client.get(f"/api/datasets/{dataset_id}/versions").json()
    assert [row["id"] for row in versions] == [latest_id, old_id]
    assert dataset["latest_version"] == versions[0]
    for version in versions:
        assert client.get(f"/api/dataset-versions/{version['id']}").json() == version
        assert version["dataset_id"] == dataset_id
        assert version["dataset_key"] == dataset["key"]
        assert version["dataset_name"] == dataset["name"]
        for pair in version["language_pairs"]:
            assert pair["target_language"] == "zh" and pair["target_name"] == "中文"
            assert pair["source_name"] and pair["source_name"] != pair["source_language"]
            samples = client.get(f"/api/dataset-versions/{version['id']}/samples", params={
                "language": pair["source_language"],
            }).json()
            assert samples["total"] == pair["sample_count"]
    counts = lambda version: {row["source_language"]: row["sample_count"] for row in version["language_pairs"]}
    assert counts(versions[0]) == {"th": 2}
    assert counts(versions[1]) == {"de": 3, "th": 2, "vi": 1}
    assert client.get("/api/dataset-versions/unknown").status_code == 404
    assert client.get("/api/datasets/unknown/versions").status_code == 404


def test_context_links_to_exact_dataset_version_and_model(client, mixed_results, version_history, session_factory):
    job_id, _ = mixed_results
    dataset_id, old_id, latest_id = version_history
    with session_factory() as session:
        model = session.get(EvaluatorJob, job_id).dataset_job.task.submission.model_run
        checkpoint = model.checkpoint_name
    detail = client.get(f"/api/evaluator-jobs/{job_id}").json()
    listed = next(row for row in client.get("/api/results/page").json()["items"] if row["evaluator"]["id"] == job_id)
    for result in [detail, listed]:
        assert result["dataset"]["dataset_id"] == dataset_id
        assert result["dataset"]["dataset_version_id"] == old_id != latest_id
        assert result["dataset"]["dataset_name"]
        assert result["dataset"]["id"] != dataset_id
        assert result["task"]["checkpoint_name"] == checkpoint
        assert "prompt_version_label" in result["evaluator"]


@pytest.mark.parametrize("all_unscored", [False, True])
def test_results_list_summary_matches_detail_and_uses_job_threshold(client, mixed_results, session_factory, all_unscored):
    job_id, _ = mixed_results
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        job.evaluator_revision.default_threshold = 0 if all_unscored else 10
        if all_unscored:
            for item in session.scalars(select(EvaluationItem).where(EvaluationItem.evaluator_job_id == job_id)):
                item.status = "queued"
        session.commit()
    result = client.get("/api/results/page").json()["items"][0]
    summary = result["summary"]
    assert summary["threshold"] == (0 if all_unscored else 10)
    detail = client.get(f"/api/evaluator-jobs/{job_id}/summary", params={"threshold": summary["threshold"]}).json()
    for key, value in summary.items():
        assert value == pytest.approx(detail[key]) if isinstance(value, float) else value == detail[key]
    assert summary["total"] == 6
    assert summary["passed"] == (0 if all_unscored else 1)
    assert summary["successful"] == (0 if all_unscored else 3)
    assert summary["micro_accuracy"] == pytest.approx(0 if all_unscored else 1 / 6)
    if all_unscored:
        assert summary["unscored"] == 6 and summary["micro_mean"] is None


def test_result_filters_apply_before_pagination_and_count(client, mixed_results, version_history, session_factory):
    job_id, _ = mixed_results
    _, old_id, latest_id = version_history
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        job.status = "completed"
        extra_jobs = [EvaluatorJob(dataset_job_id=job.dataset_job_id, evaluator_revision_id=job.evaluator_revision_id,
                                  status=status) for status in ["completed", "queued", "partial_failed"]]
        session.add_all(extra_jobs)
        job.dataset_job.submission_dataset.dataset_version.dataset.key = "literal%_key"
        session.commit()
    params = {"dataset_version_id": old_id, "status_group": "completed", "q": "%_", "page_size": 1}
    pages = [client.get("/api/results/page", params={**params, "page": page}).json() for page in [1, 2, 3]]
    assert [page["total"] for page in pages] == [2, 2, 2]
    assert [len(page["items"]) for page in pages] == [1, 1, 0]
    assert pages[0]["items"][0]["evaluator"]["id"] != pages[1]["items"][0]["evaluator"]["id"]
    for group in ["active", "exception"]:
        assert client.get("/api/results/page", params={**params, "status_group": group}).json()["total"] == 1
    assert client.get("/api/results/page", params={**params, "dataset_version_id": latest_id}).json()["total"] == 0
    assert client.get("/api/results/page", params={"status_group": "invalid"}).status_code == 422
    context = client.get(f"/api/evaluator-jobs/{job_id}").json()
    for query in [context["dataset"]["version_label"], context["dataset"]["dataset_name"],
                  context["task"]["run_name"], context["task"]["model_family"], context["task"]["checkpoint_name"]]:
        assert client.get("/api/results/page", params={"q": query}).json()["total"] == 4


def test_exact_sample_id_composes_with_score_language_status_and_pagination(client, mixed_results, session_factory):
    job_id, sample_ids = mixed_results
    exact_id = "literal%_sample"
    with session_factory() as session:
        dataset = session.get(EvaluatorJob, job_id).dataset_job.submission_dataset
        version_id = dataset.dataset_version_id
        for original_id, replacement in zip(sample_ids[:2], [exact_id, "literalXXsample"], strict=True):
            sample = session.scalar(select(DatasetSample).where(
                DatasetSample.dataset_version_id == version_id, DatasetSample.sample_id == original_id,
            ))
            prediction = session.scalar(select(Prediction).where(
                Prediction.submission_dataset_id == dataset.id, Prediction.sample_id == original_id,
            ))
            sample.sample_id = prediction.sample_id = replacement
        session.commit()
    for endpoint, extra in [
        (f"/api/dataset-versions/{version_id}/samples", {}),
        (f"/api/evaluator-jobs/{job_id}/items", {"item_status": "completed", "score_operator": "eq", "score_value": 8}),
    ]:
        params = {"sample_id": exact_id, "language": "de", "page_size": 1, **extra}
        first = client.get(endpoint, params=params).json()
        assert first["total"] == 1 and first["items"][0]["sample_id"] == exact_id
        second = client.get(endpoint, params={**params, "page": 2}).json()
        assert second["total"] == 1 and second["items"] == []
        assert client.get(endpoint, params={**params, "language": "th"}).json()["total"] == 0
        assert client.get(endpoint, params={**params, "sample_id": "%_"}).json()["total"] == 0
    endpoint = f"/api/evaluator-jobs/{job_id}/items"
    assert client.get(endpoint, params={"sample_id": exact_id, "item_status": "unscored"}).json()["total"] == 0
    assert client.get(endpoint, params={"sample_id": exact_id, "score_operator": "lt", "score_value": 8}).json()["total"] == 0


def counted_request(client, session_factory, url, **params):
    statements = []
    engine = session_factory.kw["bind"]
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get(url, params=params)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    return response.json(), len(statements)


def test_results_summary_queries_stay_batched_as_page_grows(client, mixed_results, session_factory):
    job_id, _ = mixed_results
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        for number in range(20):
            revision = EvaluatorRevision(profile_id=job.evaluator_revision.profile_id, revision=number + 2,
                                         config={}, default_threshold=number % 11)
            session.add(revision)
            session.flush()
            session.add(EvaluatorJob(dataset_job_id=job.dataset_job_id, evaluator_revision_id=revision.id))
        session.commit()
    one, small_count = counted_request(client, session_factory, "/api/results/page", page_size=1)
    many, large_count = counted_request(client, session_factory, "/api/results/page", page_size=100)
    assert one["total"] == many["total"] == len(many["items"]) == 21
    assert large_count == small_count
    assert large_count < 20
    for row in many["items"]:
        assert row["summary"]["threshold"] == row["evaluator"]["default_threshold"]
        assert row["summary"]["total"] == 6
        if row["evaluator"]["id"] != job_id:
            assert row["summary"]["passed"] == row["summary"]["micro_accuracy"] == 0


def test_dataset_language_counts_stay_batched_across_datasets(client, mixed_results, session_factory):
    _, small_count = counted_request(client, session_factory, "/api/datasets")
    with session_factory() as session:
        for number in range(15):
            dataset = Dataset(key=f"batch-{number}", name=f"Batch {number}")
            session.add(dataset)
            session.flush()
            session.add(DatasetVersion(dataset_id=dataset.id, version_label="v1", content_sha256="b" * 64,
                                       sample_count=0, source_languages=[]))
        session.commit()
    result, large_count = counted_request(client, session_factory, "/api/datasets")
    assert len(result) == 16
    assert small_count == large_count == 3
