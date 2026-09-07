from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from .validation import validate_prompt_template


def normalize_language_code(value: str) -> str:
    code = value.strip().lower()
    # ISO-style primary codes, BCP-47 subtags and FLORES-style script labels.
    # Unknown but well-formed codes remain usable without a registry update.
    if not re.fullmatch(r"[a-z]{2,8}(?:[-_][a-z0-9]{1,8})*", code):
        raise ValueError("语种代码格式无效，请使用 de、zh-Hant 或 eng_Latn 等格式")
    return code


class DatasetManifest(BaseModel):
    schema_version: Literal[1]
    dataset_key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}$")
    name: str = Field(min_length=1, max_length=200)
    version_label: str = Field(min_length=1, max_length=120)
    change_note: str = ""
    description: str = ""

    @field_validator("name", "version_label")
    @classmethod
    def reject_blank_metadata(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段不能只包含空白")
        return value


class DatasetSampleInput(BaseModel):
    sample_id: str = Field(min_length=1, max_length=240)
    source_language: str = Field(min_length=1, max_length=35)
    source_text: str = Field(min_length=1)
    reference_zh: str = Field(min_length=1)

    @field_validator("sample_id", "source_text", "reference_zh")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段不能只包含空白")
        return value

    @field_validator("source_language", mode="before")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        return normalize_language_code(value) if isinstance(value, str) else value


class InferenceDatasetManifest(BaseModel):
    dataset_key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}$")
    dataset_content_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class InferenceDetails(BaseModel):
    platform: str = Field(min_length=1, max_length=160)
    device: str = Field(default="", max_length=200)
    sdk: str = Field(default="", max_length=200)
    sdk_version: str = Field(default="", max_length=200)
    precision: str = Field(default="", max_length=200)
    mode: str = Field(min_length=1, max_length=40)
    detects_language: bool | None = None
    generated_at: datetime | None = None
    code_revision: str = ""
    decoding: dict[str, Any] = Field(default_factory=dict)

    @field_validator("platform", "mode")
    @classmethod
    def reject_blank_metadata(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段不能只包含空白")
        return value


class ResultManifest(BaseModel):
    schema_version: Literal[1]
    run_name: str = Field(min_length=1, max_length=200)
    model_family: str = Field(min_length=1, max_length=200)
    checkpoint_name: str = Field(min_length=1, max_length=240)
    model_version: str = Field(default="", max_length=120)
    model_notes: str = ""
    inference: InferenceDetails
    datasets: list[InferenceDatasetManifest] = Field(min_length=1)

    @field_validator("run_name", "model_family", "checkpoint_name")
    @classmethod
    def reject_blank_metadata(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段不能只包含空白")
        return value

    @model_validator(mode="after")
    def unique_datasets(self) -> "ResultManifest":
        keys = [item.dataset_key for item in self.datasets]
        if len(keys) != len(set(keys)):
            raise ValueError("datasets 中存在重复 dataset_key")
        return self


class PredictionInput(BaseModel):
    sample_id: str = Field(min_length=1, max_length=240)
    translation_zh: str = Field(min_length=1)
    predicted_language: str | None = Field(default=None, max_length=35)

    @field_validator("sample_id", "translation_zh")
    @classmethod
    def reject_blank_translation(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段不能只包含空白")
        return value

    @field_validator("predicted_language")
    @classmethod
    def normalize_predicted_language(cls, value: str | None) -> str | None:
        return normalize_language_code(value) if value and value.strip() else None


class PathImportRequest(BaseModel):
    path: str = Field(min_length=1)
    version_overrides: dict[str, str] = Field(default_factory=dict)


class ImportManifestRequest(BaseModel):
    manifest: dict[str, Any]


ImportOptionCategory = Literal["model", "device", "platform", "precision", "inference_mode"]


class ImportOptionUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    detects_language: bool = False
    platform: str = Field(default="", max_length=160)
    sdk: str = Field(default="", max_length=200)
    sdk_version: str = Field(default="", max_length=200)

    @field_validator("label", "platform", "sdk", "sdk_version", mode="before")
    @classmethod
    def trim_label(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class ImportOptionCreate(ImportOptionUpdate):
    category: ImportOptionCategory
    value: str = Field(min_length=1, max_length=200)

    @field_validator("value", mode="before")
    @classmethod
    def trim_value(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def valid_option(self) -> "ImportOptionCreate":
        limit = {"platform": 160, "inference_mode": 40}.get(self.category, 200)
        if len(self.value) > limit:
            raise ValueError(f"选项值不能超过 {limit} 个字符")
        if self.category != "inference_mode" and self.detects_language:
            raise ValueError("只有推理模式可以设置语种识别统计")
        if self.category != "device" and (self.platform or self.sdk or self.sdk_version):
            raise ValueError("只有设备可以设置平台和 SDK 信息")
        if (self.sdk or self.sdk_version) and not self.platform:
            raise ValueError("设置 SDK 信息时必须指定所属平台")
        return self


class EvaluatorSelection(BaseModel):
    evaluator_revision_id: str
    prompt_version_id: str | None = None


class CommitSubmissionRequest(BaseModel):
    evaluators: list[EvaluatorSelection] = Field(min_length=1)
    force_reevaluate: bool = False


class PromptProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    version_label: str = Field(default="", max_length=160)
    system_template: str = ""
    user_template: str = ""
    published: bool = True

    _validate_templates = field_validator("system_template", "user_template")(validate_prompt_template)


class PromptVersionCreate(BaseModel):
    version_label: str = Field(default="", max_length=160)
    system_template: str = ""
    user_template: str = ""
    published: bool = True

    _validate_templates = field_validator("system_template", "user_template")(validate_prompt_template)


class EvaluatorProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    evaluator_type: Literal["openai_compatible_llm", "sacrebleu_zh"]
    config: dict[str, Any]
    default_threshold: float = Field(ge=0, le=100, allow_inf_nan=False)
    enabled: bool = True


class EvaluatorRevisionCreate(BaseModel):
    config: dict[str, Any]
    default_threshold: float = Field(ge=0, le=100, allow_inf_nan=False)


class EvaluatorProfileUpdate(BaseModel):
    enabled: bool


class ThresholdSummary(BaseModel):
    evaluator_job_id: str
    threshold: float = Field(ge=0, le=100, allow_inf_nan=False)
    score_min: float
    score_max: float
    unit: str
    micro_mean: float | None
    macro_mean: float | None
    micro_accuracy: float | None
    macro_accuracy: float | None
    successful: int
    passed: int
    unscored: int
    failed: int
    cancelled: int
    total: int
    coverage: float
    by_language: list[dict[str, Any]]
    aggregates: list[dict[str, Any]]


class CompareResultsRequest(BaseModel):
    evaluator_job_ids: list[str] = Field(min_length=2, max_length=12)
    threshold: float = Field(ge=0, le=100, allow_inf_nan=False)
