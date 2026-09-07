from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .evaluators.llm import DEFAULT_SYSTEM_TEMPLATE, DEFAULT_USER_TEMPLATE
from .models import EvaluatorProfile, EvaluatorRevision, ModelRun, PromptProfile, PromptVersion
from .import_options import remember_option


def seed_defaults(session: Session) -> None:
    for category, values in {
        "model": ["qwen3-0.6b"],
        "device": ["CPU", "GPU", "NPU"],
        "platform": ["nvidia-1080ti"],
        "precision": ["fp32", "fp16", "bf16", "int8", "int4"],
    }.items():
        for value in values:
            remember_option(session, category, value)
    for value, label in [("source_language_provided", "已提供源语种"), ("auto_detect", "自动识别语种")]:
        remember_option(session, "inference_mode", value, label=label, detects_language=value == "auto_detect")
    historical_options: dict[tuple[str, str], bool] = {}
    for run in session.scalars(select(ModelRun).execution_options(yield_per=500)):
        inference = run.result_info.get("inference", {})
        for category, value in [("model", run.model_family), ("platform", run.inference_platform),
                                ("device", inference.get("device", "")), ("precision", inference.get("precision", "")),
                                ("inference_mode", run.inference_mode)]:
            if value:
                historical_options.setdefault((category, value), category == "inference_mode" and run.detects_language)
    for (category, value), detects_language in historical_options.items():
        remember_option(session, category, value, detects_language=detects_language)
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
