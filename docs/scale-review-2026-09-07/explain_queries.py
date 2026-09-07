#!/usr/bin/env python3
"""Explain captured statements without executing their result scans.

  .venv/bin/python docs/scale-review-2026-09-07/explain_queries.py /tmp/te-scale-400000.db --jobs 2
"""
import argparse
import json
from pathlib import Path
import sqlite3

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("db", type=Path)
parser.add_argument("--jobs", type=int, default=2)
args = parser.parse_args()
connection = sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True)
output = []
for case in ["items_first", "items_score", "results_one", "results_mean_one", "compare_two"]:
    path = args.db.parent / f"{args.db.stem}-{args.jobs}jobs-{case}-sql.json"
    if not path.exists():
        continue
    statements = json.loads(path.read_text())
    selected = [row for row in statements if "evaluation_items" in row["sql"] and ("dataset_samples" in row["sql"] or "score_results" in row["sql"])]
    for index, row in enumerate(selected):
        output.append(dict(case=case, statement_index=index, sql=row["sql"], params=row["params"], plan=connection.execute("EXPLAIN QUERY PLAN " + row["sql"], row["params"]).fetchall()))
print(json.dumps(output, indent=2, ensure_ascii=False))
