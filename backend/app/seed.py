from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .evaluators.llm import DEFAULT_SYSTEM_TEMPLATE, DEFAULT_USER_TEMPLATE
from .models import EvaluatorProfile, EvaluatorRevision, PromptProfile, PromptVersion


def seed_defaults(session: Session) -> None:
    prompt = session.scalar(select(PromptProfile).where(PromptProfile.name == "译中质量评分"))
    if not prompt:
        prompt = PromptProfile(name="译中质量评分", description="默认 0–10 分译中质量评审模板")
        session.add(prompt)
        session.flush()
        session.add(
            PromptVersion(
                profile_id=prompt.id,
                version=1,
                system_template=DEFAULT_SYSTEM_TEMPLATE,
                user_template=DEFAULT_USER_TEMPLATE,
                published=True,
            )
        )
    bleu = session.scalar(
        select(EvaluatorProfile).where(EvaluatorProfile.name == "SacreBLEU 中文")
    )
    if not bleu:
        bleu = EvaluatorProfile(
            name="SacreBLEU 中文", evaluator_type="sacrebleu_zh", enabled=True
        )
        session.add(bleu)
        session.flush()
        session.add(
            EvaluatorRevision(
                profile_id=bleu.id,
                revision=1,
                config={
                    "tokenize": "zh",
                    "smooth_method": "exp",
                    "effective_order": True,
                },
                default_threshold=20.0,
            )
        )
    session.commit()

