from __future__ import annotations

from typing import Any

from sacrebleu.metrics import BLEU

from .base import BaseEvaluator, PermanentEvaluatorError, ScoreInput, ScoreOutput


class SacreBleuZhEvaluator(BaseEvaluator):
    evaluator_type = "sacrebleu_zh"

    def __init__(self, config: dict[str, Any]) -> None:
        normalized = self.validate_config(config)
        super().__init__(normalized)
        self.metric = BLEU(
            tokenize=normalized["tokenize"],
            smooth_method=normalized["smooth_method"],
            smooth_value=normalized.get("smooth_value"),
            effective_order=normalized["effective_order"],
        )

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        tokenize = str(config.get("tokenize", "zh"))
        smooth_method = str(config.get("smooth_method", "exp"))
        if smooth_method not in {"none", "floor", "add-k", "exp"}:
            raise ValueError("smooth_method 必须为 none、floor、add-k 或 exp")
        return {
            "tokenize": tokenize,
            "smooth_method": smooth_method,
            "smooth_value": config.get("smooth_value"),
            "effective_order": bool(config.get("effective_order", True)),
        }

    async def evaluate_one(self, item: ScoreInput) -> ScoreOutput:
        try:
            score = float(self.metric.sentence_score(item.translation_zh, [item.reference_zh]).score)
        except Exception as exc:  # SacreBLEU exposes tokenizer-specific exceptions.
            raise PermanentEvaluatorError(str(exc)) from exc
        return ScoreOutput(
            score=score,
            score_min=0.0,
            score_max=100.0,
            unit="BLEU",
            raw_response={"tokenize": self.config["tokenize"]},
        )

    def corpus_aggregates(self, items: list[ScoreInput]) -> dict[str, float | str]:
        if not items:
            return {}
        result = self.metric.corpus_score(
            [item.translation_zh for item in items],
            [[item.reference_zh for item in items]],
        )
        return {
            "corpus_bleu": float(result.score),
            "signature": str(self.metric.get_signature()),
        }

