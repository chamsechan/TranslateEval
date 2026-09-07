from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .evaluators.llm import DEFAULT_SYSTEM_TEMPLATE, DEFAULT_USER_TEMPLATE
from .models import EvaluatorProfile, EvaluatorRevision, ModelRun, PromptProfile, PromptVersion
from .import_options import remember_option


def seed_defaults(session: Session) -> None:
    for category, values in {
        "model": ["qwen3-0.6b"],
        "precision": ["fp32", "fp16", "bf16", "int8", "int4"],
    }.items():
        for value in values:
            remember_option(session, category, value)
    for value, label in [("nvidia", "英伟达"), ("axera", "爱芯")]:
        remember_option(session, "platform", value, label=label)
    for value, label, platform in [
        ("nvidia-1080ti", "英伟达1080Ti", "nvidia"),
        ("nvidia-2080ti", "英伟达2080Ti", "nvidia"),
        ("ax650", "爱芯650", "axera"),
    ]:
        remember_option(session, "device", value, label=label, platform=platform)
    for value, label in [("default", "默认"), ("thinking", "思考"), ("non_thinking", "不思考")]:
        remember_option(session, "inference_mode", value, label=label)
    historical_options: dict[tuple[str, str], bool] = {}
    for run in session.scalars(select(ModelRun).execution_options(yield_per=500)):
        inference = run.result_info.get("inference", {})
        for category, value in [("model", run.model_family), ("platform", run.inference_platform),
                                ("precision", inference.get("precision", "")),
                                ("inference_mode", run.inference_mode)]:
            if value:
                historical_options.setdefault((category, value), category == "inference_mode" and run.detects_language)
        remember_option(session, "device", inference.get("device", ""),
                        platform=run.inference_platform, sdk=inference.get("sdk", ""),
                        sdk_version=inference.get("sdk_version", ""))
    for (category, value), detects_language in historical_options.items():
        remember_option(session, category, value, detects_language=detects_language,
                        enabled=not (category == "inference_mode" and value in {
                            "source_language_provided", "auto_detect",
                        }))
    prompt = session.scalar(select(PromptProfile).where(PromptProfile.name == "译中质量评分"))
    if not prompt:
        prompt = PromptProfile(name="译中质量评分", description="默认 0–10 分译中质量评审模板")
        session.add(prompt)
        session.flush()
        session.add(
            PromptVersion(
                profile_id=prompt.id,
                version=1,
                version_label=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d") + ".1",
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
