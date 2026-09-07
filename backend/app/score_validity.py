"""The shared interpretation of current scores, including legacy stored rows."""
from sqlalchemy import case, func

from .models import EvaluationItem, ScoreResult


def score_maximum(evaluator_type):
    if isinstance(evaluator_type, str):
        return 100 if evaluator_type == "sacrebleu_zh" else 10
    return case((evaluator_type == "sacrebleu_zh", 100), else_=10)


def comparable_item_score(evaluator_type):
    if isinstance(evaluator_type, str) and evaluator_type != "sacrebleu_zh":
        return ScoreResult.score
    is_bleu = True if isinstance(evaluator_type, str) else evaluator_type == "sacrebleu_zh"
    return case(
        (ScoreResult.score.between(100 - 1e-9, 100 + 1e-9) & is_bleu, 100.0),
        (ScoreResult.score.between(-1e-9, 1e-9) & is_bleu, 0.0),
        else_=ScoreResult.score,
    )


def scored_item_condition(evaluator_type):
    """Only completed, finite scores on the selected evaluator's scale count."""
    maximum = score_maximum(evaluator_type)
    unit = ("BLEU" if evaluator_type == "sacrebleu_zh" else "point") if isinstance(evaluator_type, str) else case((evaluator_type == "sacrebleu_zh", "BLEU"), else_="point")
    return func.coalesce(
        (EvaluationItem.status == "completed")
        & ScoreResult.id.is_not(None)
        & (ScoreResult.evaluator_type == evaluator_type)
        & (ScoreResult.score_min == 0)
        & (ScoreResult.score_max == maximum)
        & (ScoreResult.unit == unit)
        & comparable_item_score(evaluator_type).between(0, maximum),
        False,
    )
