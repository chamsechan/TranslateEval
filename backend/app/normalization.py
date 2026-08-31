from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any


def normalize_text(value: str) -> str:
    """Canonicalize representation without hiding meaningful whitespace differences."""
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def sample_content_hash(
    sample_id: str, source_language: str, source_text: str, reference_zh: str
) -> str:
    payload = [sample_id, source_language.lower(), normalize_text(source_text), normalize_text(reference_zh)]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def dataset_content_hash(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item["sample_id"]):
        digest.update(
            sample_content_hash(
                row["sample_id"],
                row["source_language"],
                row["source_text"],
                row["reference_zh"],
            ).encode("ascii")
        )
        digest.update(b"\n")
    return digest.hexdigest()

