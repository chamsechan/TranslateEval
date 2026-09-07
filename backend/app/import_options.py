from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .models import ImportOption, ModelRun
from .schemas import ResultManifest


def remember_option(session: Session, category: str, value: str, *, label: str | None = None,
                    detects_language: bool = False, platform: str = "", sdk: str = "",
                    sdk_version: str = "", enabled: bool = True) -> None:
    if not value:
        return
    exists = session.scalar(select(ImportOption).where(
        ImportOption.category == category, ImportOption.value == value,
    ))
    if exists is None:
        session.add(ImportOption(category=category, value=value, label=label or value,
                                 detects_language=detects_language, enabled=enabled,
                                 platform=platform, sdk=sdk, sdk_version=sdk_version))
        session.flush()
    elif category == "device" and not exists.deleted and exists.platform in {"", platform}:
        # Import metadata can complete an older device entry, but it must not
        # replace maintained settings or mix SDK versions across platforms.
        if not exists.platform:
            exists.platform = platform
        if not exists.sdk:
            exists.sdk = sdk
        if exists.sdk == sdk and not exists.sdk_version:
            exists.sdk_version = sdk_version


def has_results_expression():
    """Correlate maintained values with immutable metadata on imported model runs."""
    return select(ModelRun.id).where(or_(
        and_(ImportOption.category == "model", ImportOption.value == ModelRun.model_family),
        and_(ImportOption.category == "platform", ImportOption.value == ModelRun.inference_platform),
        and_(ImportOption.category == "inference_mode", ImportOption.value == ModelRun.inference_mode),
        and_(ImportOption.category == "device",
             ImportOption.value == ModelRun.result_info["inference"]["device"].as_string()),
        and_(ImportOption.category == "precision",
             ImportOption.value == ModelRun.result_info["inference"]["precision"].as_string()),
    )).correlate(ImportOption).exists()


def option_has_results(session: Session, option: ImportOption) -> bool:
    return bool(session.scalar(select(has_results_expression()).select_from(ImportOption).where(
        ImportOption.id == option.id,
    )))


def snapshot_inference_mode(session: Session, manifest: ResultManifest) -> None:
    # Thinking controls are independent of language-detection metadata. Legacy
    # modes keep their existing statistics behavior for older result manifests.
    if manifest.inference.mode in {"default", "thinking", "non_thinking"}:
        if manifest.inference.detects_language is None:
            manifest.inference.detects_language = False
        return
    option = session.scalar(select(ImportOption).where(
        ImportOption.category == "inference_mode", ImportOption.value == manifest.inference.mode,
        ImportOption.deleted.is_(False),
    ))
    if option:
        manifest.inference.detects_language = option.detects_language
    elif manifest.inference.detects_language is None:
        manifest.inference.detects_language = manifest.inference.mode == "auto_detect"


def remember_manifest_options(session: Session, manifest: ResultManifest) -> None:
    for category, value in (
        ("model", manifest.model_family), ("platform", manifest.inference.platform),
        ("precision", manifest.inference.precision),
        ("inference_mode", manifest.inference.mode),
    ):
        remember_option(session, category, value, detects_language=(
            category == "inference_mode" and manifest.inference.detects_language is True
        ), enabled=not (category == "inference_mode" and value in {
            "source_language_provided", "auto_detect",
        }))
    remember_option(session, "device", manifest.inference.device,
                    platform=manifest.inference.platform, sdk=manifest.inference.sdk,
                    sdk_version=manifest.inference.sdk_version)
