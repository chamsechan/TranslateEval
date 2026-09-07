from __future__ import annotations

from typing import Any

from sacrebleu.metrics import BLEU

from .base import BaseEvaluator, PermanentEvaluatorError, ScoreInput, ScoreOutput, finite_number, validate_score_output


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
        tokenize = config.get("tokenize", "zh")
        if tokenize not in BLEU.TOKENIZERS:
            raise ValueError("未知 Tokenizer，请选择: " + ", ".join(BLEU.TOKENIZERS))
        smooth_method = config.get("smooth_method", "exp")
        if not isinstance(smooth_method, str):
            raise ValueError("smooth_method 必须是文本")
        if smooth_method not in {"none", "floor", "add-k", "exp"}:
            raise ValueError("smooth_method 必须为 none、floor、add-k 或 exp")
        effective_order = config.get("effective_order", True)
        if not isinstance(effective_order, bool):
            raise ValueError("effective_order 必须是布尔值")
        smooth_value = config.get("smooth_value")
        if smooth_value is not None:
            smooth_value = finite_number(smooth_value, "smooth_value", 1e-12, 1 if smooth_method == "floor" else 1e6)
            if smooth_method not in {"floor", "add-k"}:
                raise ValueError("仅 floor 和 add-k 支持 smooth_value")
        normalized = {
            "tokenize": tokenize,
            "smooth_method": smooth_method,
            "smooth_value": smooth_value,
            "effective_order": effective_order,
        }
        for name, default, minimum, maximum in [("concurrency", 8, 1, 64), ("max_retries", 0, 0, 10)]:
            if name in config:
                normalized[name] = finite_number(config[name], name, minimum, maximum, integer=True)
        return normalized

    async def evaluate_one(self, item: ScoreInput) -> ScoreOutput:
        try:
            score = float(self.metric.sentence_score(item.translation_zh, [item.reference_zh]).score)
        except Exception as exc:  # SacreBLEU exposes tokenizer-specific exceptions.
            raise PermanentEvaluatorError(str(exc)) from exc
        return validate_score_output(ScoreOutput(
            score=score,
            score_min=0.0,
            score_max=100.0,
            unit="BLEU",
            raw_response={"tokenize": self.config["tokenize"]},
        ), self.evaluator_type)

    def segment_statistics(self, translation: str, reference: str) -> list[int]:
        """SacreBLEU sufficient statistics; sum these before smoothing/BP.

        Kept behind this adapter because SacreBLEU currently exposes segment
        extraction privately. Regression tests compare against corpus_score.
        """
        self.metric.num_refs = 1
        return self.metric._compute_segment_statistics(
            self.metric._preprocess_segment(translation),
            self.metric._extract_reference_info([self.metric._preprocess_segment(reference)]),
        )

    def corpus_from_statistics(self, statistics: list[int]) -> dict[str, float | str]:
        self.metric.num_refs = 1
        score = self.metric._compute_score_from_stats(statistics)
        value = validate_score_output(ScoreOutput(float(score.score), 0, 100, "BLEU"), self.evaluator_type).score
        return {"corpus_bleu": value, "signature": str(self.metric.get_signature())}

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
