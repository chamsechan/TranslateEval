from __future__ import annotations

import math
from string import Formatter


PROMPT_FIELDS = {"source_language", "source_text", "reference_zh", "translation_zh"}


def validate_prompt_template(value: str) -> str:
    try:
        for _, name, spec, conversion in Formatter().parse(value):
            if name is not None and (name not in PROMPT_FIELDS or spec or conversion):
                raise ValueError("仅支持声明的四个变量；JSON 示例中的花括号请写成 {{ 和 }}")
        value.format_map(dict.fromkeys(PROMPT_FIELDS, "示例文本"))
    except (ValueError, KeyError, IndexError) as exc:
        raise ValueError(f"Prompt 模板无效：{exc}") from exc
    return value


def validate_threshold(evaluator_type: str, threshold: float) -> None:
    maximum = 100 if evaluator_type == "sacrebleu_zh" else 10
    if not math.isfinite(threshold) or not 0 <= threshold <= maximum:
        raise ValueError(f"该评价器阈值必须在 0–{maximum} 之间")
