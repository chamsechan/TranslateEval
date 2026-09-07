from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ImportOption
from .schemas import ResultManifest


def remember_option(session: Session, category: str, value: str, *, label: str | None = None,
                    detects_language: bool = False) -> None:
    if not value:
        return
    exists = session.scalar(select(ImportOption).where(
        ImportOption.category == category, ImportOption.value == value,
    ))
    if exists is None:
        session.add(ImportOption(category=category, value=value, label=label or value,
                                 detects_language=detects_language))
        session.flush()


def snapshot_inference_mode(session: Session, manifest: ResultManifest) -> None:
    option = session.scalar(select(ImportOption).where(
        ImportOption.category == "inference_mode", ImportOption.value == manifest.inference.mode,
    ))
    if option:
        manifest.inference.detects_language = option.detects_language
    elif manifest.inference.detects_language is None:
        manifest.inference.detects_language = manifest.inference.mode == "auto_detect"


def remember_manifest_options(session: Session, manifest: ResultManifest) -> None:
    for category, value in (
        ("model", manifest.model_family), ("platform", manifest.inference.platform),
        ("device", manifest.inference.device), ("precision", manifest.inference.precision),
        ("inference_mode", manifest.inference.mode),
    ):
        remember_option(session, category, value, detects_language=(
            category == "inference_mode" and manifest.inference.detects_language is True
        ))
