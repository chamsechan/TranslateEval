"""Probe existing LLM cache lookup with synthetic history; never calls a service."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    with tempfile.TemporaryDirectory(prefix="translate-cache-scale-") as directory:
        root = Path(directory)
        os.environ["TRANSLATION_EVAL_DATABASE_URL"] = f"sqlite:///{root / 'cache.db'}"
        os.environ["TRANSLATION_EVAL_IMPORT_DIR"] = str(root / "imports")
        from sqlalchemy import insert, select, text, tuple_
        from app.database import Base, SessionLocal, engine
        from app.models import EvaluatorProfile, EvaluatorRevision, ScoreResult
        from app.queue import _preload_llm_cache

        Base.metadata.create_all(engine)
        with SessionLocal() as session:
            profile = EvaluatorProfile(name="Synthetic cache history", evaluator_type="openai_compatible_llm")
            session.add(profile)
            session.flush()
            revision = EvaluatorRevision(profile_id=profile.id, revision=1, config={}, default_threshold=8)
            session.add(revision)
            session.commit()
            revision_id = revision.id

        def key(index):
            return f"x-{index % 40:02}", f"{index:064x}", "a" * 64, f"{index + 1:064x}"

        def rows(indices):
            return [dict(sample=SimpleNamespace(source_language=k[0], source_hash=k[1], reference_hash=k[2]),
                         prediction=SimpleNamespace(translation_hash=k[3])) for k in map(key, indices)]

        now = datetime.now(UTC)
        previous = 0
        evidence = []
        for count in (24000, 400000):
            started = time.perf_counter()
            with engine.begin() as connection:
                for offset in range(previous, count, 2000):
                    payload = []
                    for i in range(offset, min(offset + 2000, count)):
                        language, source, reference, translation = key(i)
                        payload.append(dict(id=f"synthetic-score-{i}", evaluator_type="openai_compatible_llm",
                            evaluator_revision_id=revision_id, source_language=language, source_hash=source,
                            reference_hash=reference, translation_hash=translation, evaluator_model="scale-judge",
                            base_url="http://unused.invalid", score=9, score_min=0, score_max=10, unit="point",
                            reason="合成评分理由", raw_response={"score": 9}, created_at=now))
                    connection.execute(insert(ScoreResult), payload)
            insertion = time.perf_counter() - started
            keys = [key(i) for i in range(200)]
            statement = select(ScoreResult).where(ScoreResult.evaluator_type == "openai_compatible_llm",
                ScoreResult.evaluator_model == "scale-judge",
                tuple_(ScoreResult.source_language, ScoreResult.source_hash, ScoreResult.reference_hash,
                       ScoreResult.translation_hash).in_(keys)).order_by(ScoreResult.created_at.desc())
            with engine.connect() as connection:
                plan = [list(row) for row in connection.execute(text("EXPLAIN QUERY PLAN " + str(
                    statement.compile(engine, compile_kwargs={"literal_binds": True}))))]
            timings = {}
            for name, indices in (("hit_200", range(200)), ("miss_200", range(count, count + 200))):
                measurements = []
                for _ in range(2):
                    with SessionLocal() as session:
                        started = time.perf_counter()
                        cached = _preload_llm_cache(session, "scale-judge", rows(indices))
                        measurements.append(dict(seconds=round(time.perf_counter() - started, 4), hits=len(cached)))
                timings[name] = measurements
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            result = dict(history_rows=count, added_rows=count-previous, insertion_seconds=round(insertion, 3),
                          database_mib=round((root / "cache.db").stat().st_size / 1024**2, 2),
                          query_plan=plan, measurements=timings,
                          condition="One model; 40 synthetic labels; one score per distinct key; 200-key batches; short reason/raw response.")
            if count == 400000:
                with engine.begin() as connection:
                    connection.exec_driver_sql("ANALYZE")
                with engine.connect() as connection:
                    result["post_analyze_plan"] = [list(row) for row in connection.execute(text("EXPLAIN QUERY PLAN " + str(
                        statement.compile(engine, compile_kwargs={"literal_binds": True}))))]
                with SessionLocal() as session:
                    started = time.perf_counter()
                    cached = _preload_llm_cache(session, "scale-judge", rows(range(200)))
                    result["post_analyze_hit_200"] = dict(seconds=round(time.perf_counter() - started, 4), hits=len(cached))
            evidence.append(result)
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr, flush=True)
            previous = count
        engine.dispose()
        print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
