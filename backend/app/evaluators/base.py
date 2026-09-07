from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import math
import json
from typing import Any


@dataclass(frozen=True)
class ScoreInput:
    source_language: str
    source_text: str
    reference_zh: str
    translation_zh: str


@dataclass(frozen=True)
class ScoreOutput:
    score: float
    score_min: float
    score_max: float
    unit: str
    reason: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)


class EvaluatorError(RuntimeError):
    pass


class RetriableEvaluatorError(EvaluatorError):
    pass


class PermanentEvaluatorError(EvaluatorError):
    pass


def finite_number(value: Any, name: str, minimum: float, maximum: float, *, integer: bool = False) -> float | int:
    """Configuration is JSON: do not silently coerce strings, booleans or fractions."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是{'整数' if integer else '数值'}")
    try:
        valid = math.isfinite(value) and minimum <= value <= maximum and (not integer or float(value).is_integer())
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} 必须为 {minimum:g}–{maximum:g} 之间的有限{'整数' if integer else '数值'}")
    return int(value) if integer else float(value)


def validate_score_output(output: ScoreOutput, evaluator_type: str | None = None) -> ScoreOutput:
    """The single persistence boundary for judge responses and reusable scores."""
    values = (output.score, output.score_min, output.score_max)
    try:
        valid_values = all(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
                           for value in values)
    except OverflowError:
        valid_values = False
    if not valid_values:
        raise PermanentEvaluatorError("评分及量表范围必须是有限数值")
    if output.score_min >= output.score_max or not isinstance(output.unit, str) or not output.unit.strip():
        raise PermanentEvaluatorError("评分量表范围或单位无效")
    expected = {"sacrebleu_zh": (0, 100, "BLEU"), "openai_compatible_llm": (0, 10, "point")}.get(evaluator_type)
    if expected and (output.score_min, output.score_max, output.unit) != expected:
        raise PermanentEvaluatorError("评分量表与评价器类型不一致")
    # SacreBLEU can return 100.00000000000004 for exact matches.
    tolerance = 1e-9 if evaluator_type == "sacrebleu_zh" else 0
    if not output.score_min - tolerance <= output.score <= output.score_max + tolerance:
        raise PermanentEvaluatorError("评分超出量表范围")
    if not isinstance(output.reason, str) or not isinstance(output.raw_response, dict):
        raise PermanentEvaluatorError("评分理由必须是文本，原始响应必须是 JSON 对象")
    try:
        json.dumps(output.raw_response, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PermanentEvaluatorError("原始评分响应包含无效 JSON 数值或类型") from exc
    return ScoreOutput(min(max(float(output.score), output.score_min), output.score_max),
                       float(output.score_min), float(output.score_max), output.unit,
                       output.reason, output.raw_response)


class BaseEvaluator(ABC):
    evaluator_type: str

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @classmethod
    @abstractmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @property
    def model_name(self) -> str:
        return ""

    @property
    def base_url(self) -> str:
        return ""

    @abstractmethod
    async def evaluate_one(self, item: ScoreInput) -> ScoreOutput:
        raise NotImplementedError

    def corpus_aggregates(self, items: list[ScoreInput]) -> dict[str, float | str]:
        return {}

    async def close(self) -> None:
        return None
