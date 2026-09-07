from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import DatasetSample, EvaluationItem, EvaluatorJob, EvaluatorProfile, EvaluatorRevision, Prediction, ScoreResult
from app.schemas import ThresholdSummary
from test_audit_regressions import client, task_job  # shared isolated API fixture


@pytest.fixture
def mixed_results(session_factory):
    """Three unequal language groups, including one without any scored sample."""
    _, job_id = task_job(session_factory)
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        profile = EvaluatorProfile(name="Inspection judge", evaluator_type="openai_compatible_llm")
        session.add(profile)
        session.flush()
        revision = EvaluatorRevision(profile_id=profile.id, revision=1, config={}, default_threshold=8)
        session.add(revision)
        session.flush()
        job.evaluator_revision = revision
        dataset = job.dataset_job.submission_dataset
        samples = session.scalars(
            select(DatasetSample).where(DatasetSample.dataset_version_id == dataset.dataset_version_id)
            .order_by(DatasetSample.id)
        ).all()
        states = [("de", "completed", 8), ("de", "completed", 0), ("de", "failed", None),
                  ("th", "completed", 10), ("th", "cancelled", None), ("vi", None, None)]
        for sample, (language, status, value) in zip(samples, states, strict=True):
            sample.source_language = language
            prediction = session.scalar(select(Prediction).where(
                Prediction.submission_dataset_id == dataset.id, Prediction.sample_id == sample.sample_id,
            ))
            item = session.scalar(select(EvaluationItem).where(
                EvaluationItem.evaluator_job_id == job_id, EvaluationItem.prediction_id == prediction.id,
            ))
            if status is None:
                session.delete(item)
                continue
            item.status = status
            if value is not None:
                score = ScoreResult(
                    evaluator_type="openai_compatible_llm", evaluator_revision_id=revision.id,
                    source_language=language, source_hash=sample.source_hash, reference_hash=sample.reference_hash,
                    translation_hash=prediction.translation_hash, score=value, score_min=0, score_max=10, unit="point",
                )
                session.add(score)
                session.flush()
                item.score_result_id = score.id
        session.commit()
        return job_id, [sample.sample_id for sample in samples]


def test_accuracy_uses_all_samples_and_all_languages(client, mixed_results):
    job_id, _ = mixed_results
    response = client.get(f"/api/evaluator-jobs/{job_id}/summary", params={"threshold": 8})
    assert response.status_code == 200
    summary = response.json()
    ThresholdSummary.model_validate(summary)
    assert (summary["total"], summary["successful"], summary["passed"], summary["unscored"]) == (6, 3, 2, 3)
    assert summary["micro_accuracy"] == pytest.approx(2 / 6)
    assert summary["macro_accuracy"] == pytest.approx((1 / 3 + 1 / 2 + 0) / 3)
    assert summary["micro_mean"] == 6
    assert summary["macro_mean"] == 7
    assert summary["coverage"] == 0.5
    languages = {row["source_language"]: row for row in summary["by_language"]}
    assert languages["de"]["accuracy"] == pytest.approx(1 / 3)
    assert languages["th"]["accuracy"] == 0.5
    assert languages["vi"]["accuracy"] == 0
    assert languages["vi"]["mean"] is None
    assert languages["vi"]["unscored"] == languages["vi"]["total"] == 1
    for row in languages.values():
        detail = client.get(f"/api/evaluator-jobs/{job_id}/items", params={"language": row["source_language"]}).json()
        unscored = client.get(f"/api/evaluator-jobs/{job_id}/items", params={
            "language": row["source_language"], "item_status": "unscored",
        }).json()
        assert detail["total"] == row["total"]
        assert unscored["total"] == row["unscored"]
    at_zero = client.get(f"/api/evaluator-jobs/{job_id}/summary", params={"threshold": 0}).json()
    assert at_zero["passed"] == 3 and at_zero["micro_accuracy"] == 0.5
    at_maximum = client.get(f"/api/evaluator-jobs/{job_id}/summary", params={"threshold": 10}).json()
    assert at_maximum["passed"] == 1


def test_all_unscored_samples_have_zero_accuracy_without_changing_status(client, mixed_results, session_factory):
    job_id, _ = mixed_results
    with session_factory() as session:
        items = session.scalars(select(EvaluationItem).where(EvaluationItem.evaluator_job_id == job_id)).all()
        for item in items:
            if item.status == "completed":
                item.status = "running"
        session.commit()
        before = {item.id: item.status for item in items}
        job_status = session.get(EvaluatorJob, job_id).status
    summary = client.get(f"/api/evaluator-jobs/{job_id}/summary", params={"threshold": 0}).json()
    assert summary["micro_accuracy"] == summary["macro_accuracy"] == 0
    assert summary["successful"] == summary["passed"] == summary["coverage"] == 0
    assert summary["unscored"] == summary["total"] == 6
    assert summary["micro_mean"] is None and summary["macro_mean"] is None
    assert all(row["accuracy"] == 0 for row in summary["by_language"])
    details = client.get(f"/api/evaluator-jobs/{job_id}/items", params={"item_status": "unscored"}).json()
    assert details["total"] == 6
    assert all(row["score"] is None and not row["scored"] for row in details["items"])
    with session_factory() as session:
        after = {item.id: item.status for item in session.scalars(select(EvaluationItem).where(EvaluationItem.evaluator_job_id == job_id))}
        assert after == before
        assert session.get(EvaluatorJob, job_id).status == job_status


@pytest.mark.parametrize(("operator", "value", "expected"), [
    ("eq", 0, [0]), ("eq", 8, [8]), ("eq", 10, [10]), ("eq", 7.5, []),
    ("lt", 8, [0]), ("lte", 8, [0, 8]), ("gt", 8, [10]), ("gte", 8, [8, 10]),
])
def test_numeric_filter_boundaries(client, mixed_results, operator, value, expected):
    job_id, _ = mixed_results
    response = client.get(f"/api/evaluator-jobs/{job_id}/items", params={
        "score_operator": operator, "score_value": value, "sort": "score",
    })
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == len(expected)
    assert [row["score"] for row in result["items"]] == expected


def test_filters_apply_before_count_and_pagination(client, mixed_results):
    job_id, _ = mixed_results
    params = {"language": "de", "item_status": "completed", "score_operator": "lte", "score_value": 8,
              "sort": "score", "direction": "desc", "page_size": 1}
    pages = [client.get(f"/api/evaluator-jobs/{job_id}/items", params={**params, "page": page}).json() for page in [1, 2, 3]]
    assert [page["total"] for page in pages] == [2, 2, 2]
    assert [row["score"] for page in pages for row in page["items"]] == [8, 0]
    assert pages[2]["items"] == []
    assert pages[0]["items"][0]["id"] != pages[1]["items"][0]["id"]
    params["item_status"] = "unscored"
    assert client.get(f"/api/evaluator-jobs/{job_id}/items", params=params).json()["total"] == 0


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "running", "completed"])
def test_unscored_filter_includes_incomplete_and_missing_scores(client, mixed_results, session_factory, status):
    job_id, sample_ids = mixed_results
    with session_factory() as session:
        item = session.scalar(select(EvaluationItem).join(Prediction).where(
            EvaluationItem.evaluator_job_id == job_id, Prediction.sample_id == sample_ids[0],
        ))
        item.status = status
        if status == "completed":
            item.score_result_id = None
        session.commit()
    unscored = client.get(f"/api/evaluator-jobs/{job_id}/items", params={"item_status": "unscored", "language": "de"}).json()
    assert unscored["total"] == 2
    changed = next(row for row in unscored["items"] if row["sample_id"] == sample_ids[0])
    assert changed["status"] == status
    assert changed["score"] is None and not changed["scored"]
    numeric = client.get(f"/api/evaluator-jobs/{job_id}/items", params={"score_operator": "eq", "score_value": 8}).json()
    assert numeric["total"] == 0
    exact_status = client.get(f"/api/evaluator-jobs/{job_id}/items", params={"item_status": status}).json()
    assert all(row["status"] == status for row in exact_status["items"])
    assert (sample_ids[0] in [row["sample_id"] for row in exact_status["items"]]) == (status != "completed")


def test_missing_prediction_remains_visible_and_unscored(client, mixed_results, session_factory):
    job_id, sample_ids = mixed_results
    with session_factory() as session:
        dataset_id = session.get(EvaluatorJob, job_id).dataset_job.submission_dataset_id
        prediction = session.scalar(select(Prediction).where(
            Prediction.submission_dataset_id == dataset_id, Prediction.sample_id == sample_ids[-1],
        ))
        session.delete(prediction)
        session.commit()
    detail = client.get(f"/api/evaluator-jobs/{job_id}/items", params={"item_status": "unscored", "language": "vi"}).json()
    assert detail["total"] == 1
    row = detail["items"][0]
    assert row["sample_id"] == sample_ids[-1]
    assert row["id"].startswith("sample:")
    assert row["status"] == "unscored" and row["score"] is None
    assert row["source_text"] and row["reference_zh"] and row["translation_zh"] == ""
    assert row["attempts"] == 0 and row["cache_hit"] is False
    summary = client.get(f"/api/evaluator-jobs/{job_id}/summary", params={"threshold": 8}).json()
    assert summary["total"] == 6 and summary["unscored"] == 3


@pytest.mark.parametrize("value", [-1, 11, float("inf"), float("-inf")])
def test_invalid_stored_scores_are_unscored(client, mixed_results, session_factory, value):
    job_id, sample_ids = mixed_results
    with session_factory() as session:
        score = session.scalar(select(ScoreResult).join(EvaluationItem).join(Prediction).where(
            EvaluationItem.evaluator_job_id == job_id, Prediction.sample_id == sample_ids[0],
        ))
        score.score = value
        session.commit()
    summary = client.get(f"/api/evaluator-jobs/{job_id}/summary", params={"threshold": 8}).json()
    assert summary["successful"] == 2 and summary["unscored"] == 4 and summary["passed"] == 1
    details = client.get(f"/api/evaluator-jobs/{job_id}/items", params={"item_status": "unscored"}).json()
    assert sample_ids[0] in [row["sample_id"] for row in details["items"]]
    assert all(row["score"] is None for row in details["items"])
    completed = client.get(f"/api/evaluator-jobs/{job_id}/items", params={"item_status": "completed"}).json()
    assert completed["total"] == summary["successful"]


@pytest.mark.parametrize("params", [
    {"score_operator": "eq"}, {"score_value": 0},
    {"score_operator": "eq", "score_value": "nan"}, {"score_operator": "eq", "score_value": "inf"},
    {"score_operator": "eq", "score_value": -1}, {"score_operator": "eq", "score_value": 11},
])
def test_score_filter_requires_pair_and_valid_scale(client, mixed_results, params):
    job_id, _ = mixed_results
    response = client.get(f"/api/evaluator-jobs/{job_id}/items", params=params)
    assert response.status_code == 400


def test_filter_enum_validation_and_bleu_scale(client, session_factory):
    _, job_id = task_job(session_factory)
    endpoint = f"/api/evaluator-jobs/{job_id}/items"
    assert client.get(endpoint, params={"score_operator": "invalid", "score_value": 8}).status_code == 422
    assert client.get(endpoint, params={"item_status": "invalid"}).status_code == 422
    assert client.get(endpoint, params={"score_operator": "lte", "score_value": 100}).status_code == 200
    assert client.get(endpoint, params={"score_operator": "lte", "score_value": 101}).status_code == 400


@pytest.mark.parametrize("value", [100.00000000000004, 99.99999999999996])
def test_bleu_perfect_score_roundoff_is_counted_and_filterable(client, mixed_results, session_factory, value):
    job_id, sample_ids = mixed_results
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        job.evaluator_revision.profile.evaluator_type = "sacrebleu_zh"
        for stored in session.scalars(select(ScoreResult).join(EvaluationItem).where(EvaluationItem.evaluator_job_id == job_id)):
            stored.evaluator_type = "sacrebleu_zh"
            stored.score_max = 100
            stored.unit = "BLEU"
        score = session.scalar(select(ScoreResult).join(EvaluationItem).join(Prediction).where(
            EvaluationItem.evaluator_job_id == job_id, Prediction.sample_id == sample_ids[0],
        ))
        score.score = value
        session.commit()
    summary = client.get(f"/api/evaluator-jobs/{job_id}/summary", params={"threshold": 100}).json()
    assert summary["successful"] == 3 and summary["passed"] == 1
    details = client.get(f"/api/evaluator-jobs/{job_id}/items", params={"score_operator": "eq", "score_value": 100}).json()
    assert details["total"] == 1 and details["items"][0]["score"] == 100
    assert details["items"][0]["sample_id"] == sample_ids[0]
