"""Bounded-memory, disposable indexes for reviewing and committing JSONL files.

The index is a private snapshot, never part of the application database. Hashes
retain the original order-independent format, including historical prediction
manifests, so optimizing ingestion does not invalidate existing versions.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .normalization import normalize_text, sample_content_hash, sha256_text

MAX_REPORTED_ERRORS = 1_000
IMPORT_BATCH_SIZE = 1_000


def iter_jsonl(path: Path, model: type[BaseModel], errors: list[dict[str, Any]]) -> Iterator[BaseModel]:
    def error(message: str, line: int | None = None) -> None:
        if len(errors) < MAX_REPORTED_ERRORS:
            errors.append({"file": path.name, **({"line": line} if line is not None else {}), "message": message})

    if not path.is_file():
        error("文件不存在")
        return
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    yield model.model_validate_json(line)
                except (ValidationError, json.JSONDecodeError) as exc:
                    error(str(exc), number)
    except UnicodeDecodeError as exc:
        error(f"文件不是有效 UTF-8: {exc}")


class ImportIndex:
    """Read each source once; sort, compare and replay using disk, not ORM lists."""

    def __init__(self, path: Path, model: type[BaseModel], *, dataset: bool = False):
        self.directory = tempfile.TemporaryDirectory(prefix="translateeval-import-index-")
        self.connection = sqlite3.connect(str(Path(self.directory.name) / "rows.sqlite"))
        self.errors: list[dict[str, Any]] = []
        self.count = 0
        self.language_counts: Counter[str] = Counter()
        self.dataset = dataset
        try:
            # This file is disposable and is never published. Its complete source
            # remains in the staging directory if the process is interrupted.
            self.connection.execute("PRAGMA journal_mode=OFF")
            self.connection.execute("PRAGMA synchronous=OFF")
            self.connection.execute("PRAGMA cache_size=-4096")
            self.connection.execute("PRAGMA temp_store=FILE")
            self.connection.execute("CREATE TABLE rows (sample_id TEXT NOT NULL, language TEXT, source_hash TEXT, reference_hash TEXT, content_hash TEXT, payload TEXT NOT NULL)")
            batch = []
            for item in iter_jsonl(path, model, self.errors):
                row = item.model_dump(mode="json")
                source_hash = reference_hash = content_hash = language = None
                if dataset:
                    language = row["source_language"]
                    self.language_counts[language] += 1
                    source_hash = sha256_text(row["source_text"])
                    reference_hash = sha256_text(row["reference_zh"])
                    content_hash = sample_content_hash(row["sample_id"], language, row["source_text"], row["reference_zh"])
                payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                batch.append((row["sample_id"], language, source_hash, reference_hash, content_hash, payload))
                self.count += 1
                if len(batch) == IMPORT_BATCH_SIZE:
                    self.connection.executemany("INSERT INTO rows VALUES (?, ?, ?, ?, ?, ?)", batch)
                    batch.clear()
            if batch:
                self.connection.executemany("INSERT INTO rows VALUES (?, ?, ?, ?, ?, ?)", batch)
            self.connection.execute("CREATE INDEX rows_sample_id ON rows(sample_id)")
            self.connection.commit()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> ImportIndex:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()
        self.directory.cleanup()

    def duplicate_ids(self) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT sample_id FROM rows GROUP BY sample_id HAVING COUNT(*) > 1 ORDER BY sample_id LIMIT 100"
        )]

    def content_hash(self) -> str:
        digest = hashlib.sha256()
        if self.dataset:
            for (content_hash,) in self.connection.execute("SELECT content_hash FROM rows ORDER BY sample_id"):
                digest.update(content_hash.encode("ascii"))
                digest.update(b"\n")
        else:
            digest.update(b"[")
            first = True
            for (payload,) in self.connection.execute("SELECT payload FROM rows ORDER BY sample_id"):
                if not first:
                    digest.update(b",")
                digest.update(normalize_text(payload).encode("utf-8"))
                first = False
            digest.update(b"]")
        return digest.hexdigest()

    def ids(self) -> Iterator[str]:
        for (sample_id,) in self.connection.execute("SELECT sample_id FROM rows ORDER BY sample_id"):
            yield sample_id

    def dataset_keys(self) -> Iterator[tuple[str, str, str, str]]:
        yield from self.connection.execute("SELECT sample_id, language, source_hash, reference_hash FROM rows ORDER BY sample_id")

    def batches(self) -> Iterator[list[dict[str, Any]]]:
        cursor = self.connection.execute("SELECT payload, source_hash, reference_hash, content_hash FROM rows ORDER BY rowid")
        while rows := cursor.fetchmany(IMPORT_BATCH_SIZE):
            batch = []
            for payload, source_hash, reference_hash, content_hash in rows:
                row = json.loads(payload)
                if self.dataset:
                    row.update(source_hash=source_hash, reference_hash=reference_hash, content_hash=content_hash)
                    row["source_text"] = normalize_text(row["source_text"])
                    row["reference_zh"] = normalize_text(row["reference_zh"])
                else:
                    row["translation_hash"] = sha256_text(row["translation_zh"])
                    row["translation_zh"] = normalize_text(row["translation_zh"])
                batch.append(row)
            yield batch


def compare_ids(expected: Iterator[str], actual: Iterator[str]) -> tuple[int, list[str], int, list[str]]:
    """Merge two sorted ID streams, retaining at most 100 diagnostic IDs each."""
    missing_count = unknown_count = 0
    missing: list[str] = []
    unknown: list[str] = []
    left, right = next(expected, None), next(actual, None)
    while left is not None or right is not None:
        if right is None or (left is not None and left < right):
            missing_count += 1
            if len(missing) < 100:
                missing.append(left)
            left = next(expected, None)
        elif left is None or right < left:
            unknown_count += 1
            if len(unknown) < 100:
                unknown.append(right)
            right = next(actual, None)
        else:
            left, right = next(expected, None), next(actual, None)
    return missing_count, missing, unknown_count, unknown
