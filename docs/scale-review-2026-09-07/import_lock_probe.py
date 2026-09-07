"""Run alongside import_worker_probe.py to test write lock during the 400k v2 import.

Finds only the isolated probe DB; BEGIN IMMEDIATE is always rolled back.
"""
from pathlib import Path
import json
import sqlite3
import time


def running_commit():
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            parts = path.read_bytes().split(b"\0")
        except OSError:
            continue
        if b"dataset_diff_commit" in parts and b"400000" in parts and b"--work" in parts:
            return Path(parts[parts.index(b"--work") + 1].decode()) / "test.db"


deadline = time.monotonic() + 300
database = None
while time.monotonic() < deadline:
    database = running_commit()
    if database:
        break
    time.sleep(0.1)
if database is None:
    raise SystemExit("No isolated 400k dataset_diff_commit stage found")

locked = False
while time.monotonic() < deadline:
    connection = sqlite3.connect(str(database), timeout=0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
    except sqlite3.OperationalError:
        locked = True
        break
    finally:
        connection.close()
    time.sleep(0.1)
if not locked:
    raise SystemExit("No import write lock observed before deadline")

started = time.perf_counter()
connection = sqlite3.connect(str(database), timeout=30)
evidence = dict(action="BEGIN IMMEDIATE then ROLLBACK; no committed change", stage="400k dataset_diff_commit", database_is_isolated=True, configured_timeout_seconds=30)
try:
    connection.execute("BEGIN IMMEDIATE")
    connection.rollback()
    evidence["result"] = "write transaction acquired"
except sqlite3.OperationalError as exc:
    evidence["result"] = str(exc)
finally:
    connection.close()
evidence["seconds"] = time.perf_counter() - started
Path(__file__).with_name("import-concurrent-writer-evidence.json").write_text(json.dumps(evidence, indent=2))
print(json.dumps(evidence))
