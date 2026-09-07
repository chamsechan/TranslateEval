from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Language(Base, TimestampMixin):
    __tablename__ = "languages"

    code: Mapped[str] = mapped_column(String(35), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(80), nullable=False)


class ImportOption(Base, TimestampMixin):
    __tablename__ = "import_options"
    __table_args__ = (UniqueConstraint("category", "value", name="uq_import_option_value"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    detects_language: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Dataset(Base, TimestampMixin):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    versions: Mapped[list["DatasetVersion"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", order_by="DatasetVersion.created_at"
    )


class DatasetVersion(Base, TimestampMixin):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version_label", name="uq_dataset_version_label"),
        UniqueConstraint("dataset_id", "content_sha256", name="uq_dataset_version_content"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version_label: Mapped[str] = mapped_column(String(120), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_languages: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    dataset: Mapped[Dataset] = relationship(back_populates="versions")
    samples: Mapped[list["DatasetSample"]] = relationship(
        back_populates="dataset_version", cascade="all, delete-orphan"
    )


class DatasetSample(Base):
    __tablename__ = "dataset_samples"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "sample_id", name="uq_version_sample"),
        Index("ix_samples_version_language", "dataset_version_id", "source_language"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sample_id: Mapped[str] = mapped_column(String(240), nullable=False)
    source_language: Mapped[str] = mapped_column(
        String(35), ForeignKey("languages.code"), nullable=False
    )
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    reference_zh: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    dataset_version: Mapped[DatasetVersion] = relationship(back_populates="samples")


class ModelRun(Base, TimestampMixin):
    __tablename__ = "model_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_name: Mapped[str] = mapped_column(String(200), index=True, nullable=False)
    model_family: Mapped[str] = mapped_column(String(200), index=True, nullable=False)
    checkpoint_name: Mapped[str] = mapped_column(String(240), nullable=False)
    model_version: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    inference_platform: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    inference_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    result_info: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    @property
    def detects_language(self) -> bool:
        value = self.result_info.get("inference", {}).get("detects_language")
        return value if isinstance(value, bool) else self.inference_mode == "auto_detect"


class InferenceSubmission(Base, TimestampMixin):
    __tablename__ = "inference_submissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_run_id: Mapped[str] = mapped_column(
        ForeignKey("model_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="ready", index=True, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    model_run: Mapped[ModelRun] = relationship()
    datasets: Mapped[list["SubmissionDataset"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )


class SubmissionDataset(Base):
    __tablename__ = "submission_datasets"
    __table_args__ = (
        UniqueConstraint("submission_id", "dataset_version_id", name="uq_submission_dataset"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("inference_submissions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.id"), index=True, nullable=False
    )
    dataset_key: Mapped[str] = mapped_column(String(120), nullable=False)
    prediction_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    submission: Mapped[InferenceSubmission] = relationship(back_populates="datasets")
    dataset_version: Mapped[DatasetVersion] = relationship()
    predictions: Mapped[list["Prediction"]] = relationship(
        back_populates="submission_dataset", cascade="all, delete-orphan"
    )


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint("submission_dataset_id", "sample_id", name="uq_result_sample"),
        Index("ix_predictions_dataset_sample", "submission_dataset_id", "sample_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_dataset_id: Mapped[str] = mapped_column(
        ForeignKey("submission_datasets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sample_id: Mapped[str] = mapped_column(String(240), nullable=False)
    translation_zh: Mapped[str] = mapped_column(Text, nullable=False)
    predicted_language: Mapped[str | None] = mapped_column(String(35))
    translation_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    submission_dataset: Mapped[SubmissionDataset] = relationship(back_populates="predictions")


class PromptProfile(Base, TimestampMixin):
    __tablename__ = "prompt_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    versions: Mapped[list["PromptVersion"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("profile_id", "version", name="uq_prompt_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("prompt_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_template: Mapped[str] = mapped_column(Text, nullable=False)
    user_template: Mapped[str] = mapped_column(Text, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    profile: Mapped[PromptProfile] = relationship(back_populates="versions")


class EvaluatorProfile(Base, TimestampMixin):
    __tablename__ = "evaluator_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    evaluator_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    revisions: Mapped[list["EvaluatorRevision"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class EvaluatorRevision(Base):
    __tablename__ = "evaluator_revisions"
    __table_args__ = (
        UniqueConstraint("profile_id", "revision", name="uq_evaluator_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("evaluator_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    default_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    profile: Mapped[EvaluatorProfile] = relationship(back_populates="revisions")


class EvaluationTask(Base, TimestampMixin):
    __tablename__ = "evaluation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("inference_submissions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True, nullable=False)
    force_reevaluate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    submission: Mapped[InferenceSubmission] = relationship()
    dataset_jobs: Mapped[list["DatasetJob"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class DatasetJob(Base, TimestampMixin):
    __tablename__ = "dataset_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    submission_dataset_id: Mapped[str] = mapped_column(
        ForeignKey("submission_datasets.id"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True, nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    task: Mapped[EvaluationTask] = relationship(back_populates="dataset_jobs")
    submission_dataset: Mapped[SubmissionDataset] = relationship()
    evaluator_jobs: Mapped[list["EvaluatorJob"]] = relationship(
        back_populates="dataset_job", cascade="all, delete-orphan"
    )


class EvaluatorJob(Base, TimestampMixin):
    __tablename__ = "evaluator_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_job_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    evaluator_revision_id: Mapped[str] = mapped_column(
        ForeignKey("evaluator_revisions.id"), index=True, nullable=False
    )
    prompt_version_id: Mapped[str | None] = mapped_column(ForeignKey("prompt_versions.id"))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True, nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    dataset_job: Mapped[DatasetJob] = relationship(back_populates="evaluator_jobs")
    evaluator_revision: Mapped[EvaluatorRevision] = relationship()
    prompt_version: Mapped[PromptVersion | None] = relationship()
    items: Mapped[list["EvaluationItem"]] = relationship(
        back_populates="evaluator_job", cascade="all, delete-orphan"
    )


class ScoreResult(Base):
    __tablename__ = "score_results"
    __table_args__ = (
        Index(
            "ix_score_cache_lookup",
            "evaluator_model",
            "source_language",
            "source_hash",
            "reference_hash",
            "translation_hash",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    evaluator_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    evaluator_revision_id: Mapped[str] = mapped_column(
        ForeignKey("evaluator_revisions.id"), nullable=False
    )
    prompt_version_id: Mapped[str | None] = mapped_column(ForeignKey("prompt_versions.id"))
    source_language: Mapped[str] = mapped_column(String(35), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    translation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_model: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    score_min: Mapped[float] = mapped_column(Float, nullable=False)
    score_max: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class EvaluationItem(Base):
    __tablename__ = "evaluation_items"
    __table_args__ = (
        UniqueConstraint("evaluator_job_id", "prediction_id", name="uq_job_prediction"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluator_job_id: Mapped[str] = mapped_column(
        ForeignKey("evaluator_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("predictions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    score_result_id: Mapped[str | None] = mapped_column(ForeignKey("score_results.id"))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True, nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    evaluator_job: Mapped[EvaluatorJob] = relationship(back_populates="items")
    prediction: Mapped[Prediction] = relationship()
    score_result: Mapped[ScoreResult | None] = relationship()


class AggregateScore(Base):
    __tablename__ = "aggregate_scores"
    __table_args__ = (
        UniqueConstraint(
            "evaluator_job_id", "metric_name", "source_language", name="uq_job_metric_language"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluator_job_id: Mapped[str] = mapped_column(
        ForeignKey("evaluator_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    metric_name: Mapped[str] = mapped_column(String(80), nullable=False)
    source_language: Mapped[str] = mapped_column(String(35), default="", nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ImportValidationReport(Base, TimestampMixin):
    __tablename__ = "import_validation_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="validated", nullable=False)
    staged_path: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
