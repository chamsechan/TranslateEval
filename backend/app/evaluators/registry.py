from __future__ import annotations

from typing import Any

from .base import BaseEvaluator
from .bleu import SacreBleuZhEvaluator
from .llm import OpenAICompatibleEvaluator


EVALUATORS: dict[str, type[BaseEvaluator]] = {
    SacreBleuZhEvaluator.evaluator_type: SacreBleuZhEvaluator,
    OpenAICompatibleEvaluator.evaluator_type: OpenAICompatibleEvaluator,
}


def validate_evaluator_config(evaluator_type: str, config: dict[str, Any]) -> dict[str, Any]:
    evaluator_class = EVALUATORS.get(evaluator_type)
    if not evaluator_class:
        raise ValueError(f"未知评价器类型: {evaluator_type}")
    try:
        return evaluator_class.validate_config(config)
    except (TypeError, OverflowError) as exc:
        raise ValueError("评价器配置字段类型或数值无效") from exc


def build_evaluator(
    evaluator_type: str,
    config: dict[str, Any],
    *,
    system_template: str | None = None,
    user_template: str | None = None,
) -> BaseEvaluator:
    evaluator_class = EVALUATORS.get(evaluator_type)
    if not evaluator_class:
        raise ValueError(f"未知评价器类型: {evaluator_type}")
    if evaluator_class is OpenAICompatibleEvaluator:
        kwargs: dict[str, str] = {}
        if system_template is not None:
            kwargs["system_template"] = system_template
        if user_template is not None:
            kwargs["user_template"] = user_template
        return OpenAICompatibleEvaluator(config, **kwargs)
    return evaluator_class(config)
