from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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

