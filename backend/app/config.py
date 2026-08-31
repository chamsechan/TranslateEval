from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "TRANSLATION_EVAL_DATABASE_URL",
        f"sqlite:///{PROJECT_ROOT / 'var' / 'translation_eval.db'}",
    )
    import_dir: Path = Path(
        os.getenv("TRANSLATION_EVAL_IMPORT_DIR", str(PROJECT_ROOT / "var" / "imports"))
    )
    frontend_dist: Path = PROJECT_ROOT / "frontend" / "dist"
    worker_global_concurrency: int = int(
        os.getenv("TRANSLATION_EVAL_WORKER_CONCURRENCY", "32")
    )
    worker_poll_seconds: float = float(
        os.getenv("TRANSLATION_EVAL_WORKER_POLL_SECONDS", "0.5")
    )
    worker_heartbeat_file: Path = Path(
        os.getenv(
            "TRANSLATION_EVAL_WORKER_HEARTBEAT_FILE",
            str(PROJECT_ROOT / "var" / "worker-heartbeat"),
        )
    )


settings = Settings()
