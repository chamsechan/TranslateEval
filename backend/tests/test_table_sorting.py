from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    Dataset, DatasetJob, DatasetSample, DatasetVersion, EvaluationItem, EvaluationTask, EvaluatorJob,
    EvaluatorProfile, EvaluatorRevision, InferenceSubmission, Language, ModelRun, Prediction, ScoreResult, SubmissionDataset,
)
from test_audit_regressions import client
from test_result_inspection import mixed_results
from test_result_navigation import counted_request


def all_pages(client, endpoint, **params):
    """Small pages expose sorting mistakenly performed after pagination."""
    rows = []
    page = 1
    while True:
        response = client.get(endpoint, params={**params, "page_size": 2, "page": page})
        assert response.status_code == 200, response.text
        body = response.json()
        rows.extend(body["items"])
        if len(rows) >= body["total"]:
            assert len(rows) == body["total"]
            return rows
        assert body["items"]
        page += 1


@pytest.mark.parametrize(("sort", "ascending", "descending"), [
    ("score", [1, 0, 3, 2, 4, 5], [3, 0, 1, 2, 4, 5]),
    ("source_language", [0, 1, 2, 3, 4, 5], [5, 3, 4, 0, 1, 2]),
    ("status", [0, 1, 3, 4, 2, 5], [5, 2, 4, 0, 1, 3]),
    ("verdict", [2, 4, 5, 1, 0, 3], [0, 3, 1, 2, 4, 5]),
])
def test_item_sorting_across_pages_and_stable_ties(client, mixed_results, sort, ascending, descending):
    job_id, ids = mixed_results
    endpoint = f"/api/evaluator-jobs/{job_id}/items"
    for direction, order in [("asc", ascending), ("desc", descending)]:
        rows = all_pages(client, endpoint, sort=sort, direction=direction)
        assert [row["sample_id"] for row in rows] == [ids[index] for index in order]
    assert [row["sample_id"] for row in all_pages(client, endpoint, sort="id")] == ids


def test_verdict_sort_uses_current_threshold_and_composes_with_filters(client, mixed_results):
    job_id, ids = mixed_results
    endpoint = f"/api/evaluator-jobs/{job_id}/items"
    rows = all_pages(client, endpoint, sort="verdict", threshold=10, direction="desc")
    assert [row["sample_id"] for row in rows] == [ids[index] for index in [3, 0, 1, 2, 4, 5]]
    rows = all_pages(client, endpoint, sort="score", direction="desc", language="de",
                     item_status="completed", score_operator="gte", score_value=0)
    assert [row["sample_id"] for row in rows] == ids[:2]
    for threshold in [-1, 11, "nan", "inf"]:
        assert client.get(endpoint, params={"sort": "verdict", "threshold": threshold}).status_code == 400


def test_item_text_reason_and_cache_sort_match_visible_values(client, mixed_results, session_factory):
    job_id, ids = mixed_results
    with session_factory() as session:
        items = session.execute(select(EvaluationItem, Prediction).join(Prediction).where(
            EvaluationItem.evaluator_job_id == job_id,
        )).all()
        for item, prediction in items:
            number = ids.index(prediction.sample_id)
            prediction.translation_zh = ["z", "a", "a", "b", "", ""][number]
            item.error = {1: "fallback", 2: "c"}.get(number)
            if item.score_result:
                item.score_result.reason = {0: "z", 1: "", 3: "a"}[number]
                item.cache_hit = number == 0
        missing_prediction = session.scalar(select(Prediction).where(Prediction.sample_id == ids[5]))
        session.delete(missing_prediction)
        session.commit()
    endpoint = f"/api/evaluator-jobs/{job_id}/items"
    for sort, asc, desc in [
        ("translation_zh", [1, 2, 3, 0, 4, 5], [0, 3, 1, 2, 4, 5]),
        ("reason", [3, 2, 1, 0, 4, 5], [0, 1, 2, 3, 4, 5]),
        ("cache_hit", [1, 3, 0, 2, 4, 5], [0, 1, 3, 2, 4, 5]),
    ]:
        for direction, expected in [("asc", asc), ("desc", desc)]:
            rows = all_pages(client, endpoint, sort=sort, direction=direction)
            assert [row["sample_id"] for row in rows] == [ids[number] for number in expected]


def test_dataset_sample_sorting_and_item_ids_use_full_sample_id(client, mixed_results, session_factory):
    job_id, ids = mixed_results
    replacements = ["z-final", "a-second", "a-first", "m-final", "g-final", "c-final"]
    with session_factory() as session:
        dataset = session.get(EvaluatorJob, job_id).dataset_job.submission_dataset
        version_id = dataset.dataset_version_id
        for number, sample in enumerate(session.scalars(select(DatasetSample).where(
            DatasetSample.dataset_version_id == version_id,
        ).order_by(DatasetSample.id))):
            prediction = session.scalar(select(Prediction).where(
                Prediction.submission_dataset_id == dataset.id, Prediction.sample_id == sample.sample_id,
            ))
            sample.sample_id = prediction.sample_id = replacements[number]
            sample.source_text = ["z", "a", "a", "f", "b", "d"][number]
            sample.reference_zh = ["b", "c", "d", "e", "f", "a"][number]
        session.commit()
    endpoint = f"/api/dataset-versions/{version_id}/samples"
    for sort, asc, desc in [
        ("sample_id", [2, 1, 5, 4, 3, 0], [0, 3, 4, 5, 1, 2]),
        ("source_language", [0, 1, 2, 3, 4, 5], [5, 3, 4, 0, 1, 2]),
        ("source_text", [1, 2, 4, 5, 3, 0], [0, 3, 5, 4, 1, 2]),
        ("reference_zh", [5, 0, 1, 2, 3, 4], [4, 3, 2, 1, 0, 5]),
    ]:
        for direction, expected in [("asc", asc), ("desc", desc)]:
            rows = all_pages(client, endpoint, sort=sort, direction=direction)
            assert [row["sample_id"] for row in rows] == [replacements[number] for number in expected]
    assert [row["sample_id"] for row in all_pages(client, endpoint)] == replacements
    filtered = all_pages(client, endpoint, sort="source_text", direction="desc", language="de")
    assert [row["sample_id"] for row in filtered] == replacements[:3]
    exact = all_pages(client, endpoint, sort="sample_id", sample_id=replacements[2])
    assert [row["sample_id"] for row in exact] == [replacements[2]]
    rows = all_pages(client, f"/api/evaluator-jobs/{job_id}/items", sort="sample_id", direction="asc")
    assert [row["sample_id"] for row in rows] == sorted(replacements)


@pytest.fixture
def sortable_results(session_factory):
    """Independent runs with deliberate score, threshold, date and text orders."""
    cases = [
        ("c-run", "b-data", [8, 0, None, 10, None, None], 8, "completed", 0),
        ("a-run", "c-data", [10, None, None, None, None, None], 10, "failed", 4),
        ("e-run", "a-data", [8, 8, 8, 8, 8, 8], 9, "queued", 2),
        ("d-run", "e-data", [None, None, None, None, None, None], 0, "cancelled", 1),
        ("b-run", "d-data", [8, 8, 8, 8, 8, 8], 8, "completed", 3),
    ]
    with session_factory() as session:
        profile = EvaluatorProfile(name="Sort judge", evaluator_type="openai_compatible_llm")
        session.add(profile)
        session.add(Language(code="de", name_zh="德语"))
        session.flush()
        for number, (name, key, scores, threshold, status, day) in enumerate(cases):
            dataset = Dataset(key=key, name=f"Sort {key}")
            model = ModelRun(id=f"model-{number}", run_name=name, model_family=f"family-{4-number}",
                             checkpoint_name="sort-case", model_version=f"v{number}" if number != 2 else "",
                             notes=["z", "a", "", "b", "b"][number], inference_platform="local",
                             inference_mode="auto_detect" if number % 2 else "specified",
                             result_info={}, created_at=datetime(2026, 1, 1) + timedelta(days=day))
            session.add_all([dataset, model])
            session.flush()
            version = DatasetVersion(dataset_id=dataset.id, version_label="sort-case", content_sha256="a" * 64,
                                     sample_count=len(scores), source_languages=["de"])
            submission = InferenceSubmission(model_run_id=model.id, manifest={})
            revision = EvaluatorRevision(profile_id=profile.id, revision=number + 1, config={}, default_threshold=threshold)
            session.add_all([version, submission, revision])
            session.flush()
            submitted = SubmissionDataset(submission_id=submission.id, dataset_version_id=version.id,
                                          dataset_key=key, prediction_count=len(scores), dataset_content_sha256="a" * 64)
            task = EvaluationTask(submission_id=submission.id, status=status, created_at=model.created_at)
            session.add_all([submitted, task])
            session.flush()
            dataset_job = DatasetJob(task_id=task.id, submission_dataset_id=submitted.id, status=status)
            session.add(dataset_job)
            session.flush()
            job = EvaluatorJob(id=f"job-{number}", dataset_job_id=dataset_job.id,
                               evaluator_revision_id=revision.id, status=status)
            session.add(job)
            session.flush()
            for index, value in enumerate(scores):
                sample = DatasetSample(dataset_version_id=version.id, sample_id=str(index), source_language="de",
                                       source_text="source", reference_zh="参考", source_hash="s", reference_hash="r", content_hash="c")
                session.add(sample)
                if value is None:
                    continue  # Missing predictions and items still belong in the denominator.
                prediction = Prediction(submission_dataset_id=submitted.id, sample_id=str(index),
                                        translation_zh="译文", translation_hash="t")
                score = ScoreResult(evaluator_type=profile.evaluator_type, evaluator_revision_id=revision.id,
                                    source_language="de", source_hash="s", reference_hash="r", translation_hash="t",
                                    score=value, score_min=0, score_max=10, unit="point")
                session.add_all([prediction, score])
                session.flush()
                session.add(EvaluationItem(evaluator_job_id=job.id, prediction_id=prediction.id,
                                           score_result_id=score.id, status="completed"))
        session.commit()


@pytest.mark.parametrize(("sort", "ascending", "descending"), [
    ("micro_accuracy", [2, 3, 1, 0, 4], [4, 0, 1, 2, 3]),
    ("micro_mean", [0, 2, 4, 1, 3], [1, 2, 4, 0, 3]),
    ("run_name", [1, 4, 0, 3, 2], [2, 3, 0, 4, 1]),
    ("dataset", [2, 0, 1, 4, 3], [3, 4, 1, 0, 2]),
    ("status", [2, 0, 4, 3, 1], [1, 3, 0, 4, 2]),
    ("created_at", [0, 3, 2, 4, 1], [1, 4, 2, 3, 0]),
])
def test_result_sorting_is_global_stable_and_uses_each_revision_threshold(client, sortable_results, sort, ascending, descending):
    for direction, expected in [("asc", ascending), ("desc", descending)]:
        rows = all_pages(client, "/api/results/page", sort=sort, direction=direction, q="sort-case")
        assert [row["evaluator"]["id"] for row in rows] == [f"job-{number}" for number in expected]
        for row in rows:
            summary = row["summary"]
            detail = client.get(f"/api/evaluator-jobs/{row['evaluator']['id']}/summary",
                                params={"threshold": summary["threshold"]}).json()
            for field in ["micro_accuracy", "micro_mean", "passed", "total", "successful"]:
                assert summary[field] == detail[field]
    default = all_pages(client, "/api/results/page", sort="default", direction="asc")
    assert [row["evaluator"]["id"] for row in default] == ["job-1", "job-4", "job-2", "job-3", "job-0"]


def test_result_sort_composes_with_filters_and_remains_batched(client, sortable_results, session_factory):
    endpoint = "/api/results/page"
    rows = all_pages(client, endpoint, sort="micro_accuracy", direction="desc", status_group="completed", q="sort-case")
    assert [row["evaluator"]["id"] for row in rows] == ["job-4", "job-0"]
    version_id = rows[0]["dataset"]["dataset_version_id"]
    rows = all_pages(client, endpoint, sort="micro_mean", dataset_version_id=version_id)
    assert [row["evaluator"]["id"] for row in rows] == ["job-4"]
    one, small_count = counted_request(client, session_factory, endpoint, sort="micro_accuracy", page_size=1)
    many, large_count = counted_request(client, session_factory, endpoint, sort="micro_accuracy", page_size=100)
    assert len(one["items"]) == 1 and len(many["items"]) == 5
    assert small_count == large_count < 20


def test_result_sorting_mixes_evaluator_scales_and_keeps_empty_datasets_last(client, sortable_results, session_factory):
    with session_factory() as session:
        job = session.get(EvaluatorJob, "job-1")
        bleu = EvaluatorProfile(name="Sort BLEU", evaluator_type="sacrebleu_zh")
        session.add(bleu)
        session.flush()
        job.evaluator_revision.profile_id = bleu.id
        job.evaluator_revision.default_threshold = 100
        score = session.scalar(select(ScoreResult).join(EvaluationItem).where(EvaluationItem.evaluator_job_id == job.id))
        score.evaluator_type = "sacrebleu_zh"
        score.score = 100.00000000000004
        score.score_max = 100
        score.unit = "BLEU"
        empty_version = session.get(EvaluatorJob, "job-3").dataset_job.submission_dataset.dataset_version
        for sample in session.scalars(select(DatasetSample).where(DatasetSample.dataset_version_id == empty_version.id)):
            session.delete(sample)
        empty_version.sample_count = 0
        session.commit()
    for direction, expected in [("asc", [2, 1, 0, 4, 3]), ("desc", [4, 0, 1, 2, 3])]:
        rows = all_pages(client, "/api/results/page", sort="micro_accuracy", direction=direction)
        assert [row["evaluator"]["id"] for row in rows] == [f"job-{number}" for number in expected]
        bleu_summary = next(row["summary"] for row in rows if row["evaluator"]["id"] == "job-1")
        assert bleu_summary["score_max"] == 100 and bleu_summary["passed"] == 1
        assert bleu_summary["micro_accuracy"] == pytest.approx(1 / 6)
        assert rows[-1]["summary"]["total"] == 0 and rows[-1]["summary"]["micro_accuracy"] is None


def test_result_dataset_sort_includes_version_and_default_has_stable_date_ties(client, sortable_results, session_factory):
    with session_factory() as session:
        original = session.get(EvaluatorJob, "job-0")
        other = session.get(EvaluatorJob, "job-2")
        original_version = original.dataset_job.submission_dataset.dataset_version
        other_version = other.dataset_job.submission_dataset.dataset_version
        original_version.version_label = "z-version"
        other_version.version_label = "a-version"
        other_version.content_sha256 = "b" * 64
        other_version.dataset_id = original_version.dataset_id
        other.dataset_job.submission_dataset.dataset_key = original.dataset_job.submission_dataset.dataset_key
        other.dataset_job.task.created_at = original.dataset_job.task.created_at
        other.dataset_job.task.submission.model_run.created_at = original.dataset_job.task.submission.model_run.created_at
        session.commit()
    for direction, expected in [("asc", [2, 0, 1, 4, 3]), ("desc", [3, 4, 1, 0, 2])]:
        rows = all_pages(client, "/api/results/page", sort="dataset", direction=direction)
        assert [row["evaluator"]["id"] for row in rows] == [f"job-{number}" for number in expected]
    for endpoint, field in [("/api/results/page", "evaluator"), ("/api/model-runs/page", None)]:
        rows = all_pages(client, endpoint, sort="default")
        ids = [row[field]["id"] if field else row["id"] for row in rows]
        assert [value.rsplit("-", 1)[1] for value in ids] == ["1", "4", "3", "2", "0"]


@pytest.mark.parametrize(("sort", "ascending", "descending"), [
    ("run_name", [1, 4, 0, 3, 2], [2, 3, 0, 4, 1]),
    ("model_family", [4, 3, 2, 1, 0], [0, 1, 2, 3, 4]),
    ("model_version", [0, 1, 3, 4, 2], [4, 3, 1, 0, 2]),
    ("inference_platform", [0, 1, 2, 3, 4], [0, 1, 2, 3, 4]),
    ("inference_mode", [1, 3, 0, 2, 4], [0, 2, 4, 1, 3]),
    ("notes", [1, 3, 4, 0, 2], [0, 3, 4, 1, 2]),
    ("created_at", [0, 3, 2, 4, 1], [1, 4, 2, 3, 0]),
])
def test_model_sorting_across_pages_and_empty_values_last(client, sortable_results, sort, ascending, descending):
    for direction, expected in [("asc", ascending), ("desc", descending)]:
        rows = all_pages(client, "/api/model-runs/page", sort=sort, direction=direction, q="sort-case")
        assert [row["id"] for row in rows] == [f"model-{number}" for number in expected]
    default = all_pages(client, "/api/model-runs/page", sort="default")
    assert [row["id"] for row in default] == ["model-1", "model-4", "model-2", "model-3", "model-0"]
    assert len(all_pages(client, "/api/model-runs/page", sort=sort, q="a-run")) == 1


def test_sort_parameters_are_whitelisted(client, mixed_results, session_factory):
    job_id, _ = mixed_results
    with session_factory() as session:
        version_id = session.get(EvaluatorJob, job_id).dataset_job.submission_dataset.dataset_version_id
    for endpoint in ["/api/results/page", "/api/model-runs/page", f"/api/evaluator-jobs/{job_id}/items",
                     f"/api/dataset-versions/{version_id}/samples"]:
        for params in [{"sort": "score; DROP TABLE model_runs"}, {"direction": "invalid"}]:
            assert client.get(endpoint, params=params).status_code == 422
