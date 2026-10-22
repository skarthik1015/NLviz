"""Runs every reference query on its own database and writes the frozen case file (plan step 0b).

    python eval/bench/build_cases.py --ro-password-file <nlviz_ro pw> --taxi-dir <dir with the taxi files>

tpch and employees are read with the app's read-only user (Postgres 15432, MySQL 13306); taxi with
DuckDB over the downloaded files (views `trips` and `zones`). Results are written unchanged apart
from types: decimals become floats, dates ISO strings. The case file is the frozen input of the
benchmark: change it only in a commit of its own, before measuring.
"""
from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from cases_dev import CASES  # noqa: E402

MAX_ROWS = 50


def _plain(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _runners(args):
    import duckdb
    import psycopg2
    import pymysql

    password = Path(args.ro_password_file).read_text().strip()
    pg = psycopg2.connect(host="127.0.0.1", port=15432, user="nlviz_ro", password=password, dbname="tpch")
    my = pymysql.connect(host="127.0.0.1", port=13306, user="nlviz_ro", password=password, database="employees")
    duck = duckdb.connect()
    taxi = Path(args.taxi_dir).as_posix()
    duck.execute(f"CREATE VIEW trips AS SELECT * FROM read_parquet('{taxi}/yellow_tripdata_2024-01.parquet')")
    duck.execute(f"CREATE VIEW zones AS SELECT * FROM read_csv_auto('{taxi}/taxi_zone_lookup.csv')")

    def sql_cursor(conn):
        def run(sql):
            with conn.cursor() as cur:
                cur.execute(sql)  # no parameters: neither driver formats '%' then
                return [desc[0] for desc in cur.description], cur.fetchall()
        return run

    def duck_run(sql):
        result = duck.execute(sql)
        return [desc[0] for desc in result.description], result.fetchall()

    return {"tpch": sql_cursor(pg), "employees": sql_cursor(my), "taxi": duck_run}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ro-password-file", required=True)
    parser.add_argument("--taxi-dir", required=True)
    parser.add_argument("--out", default=str(HERE / "cases" / "dev.jsonl"))
    args = parser.parse_args()
    runners = _runners(args)

    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    problems = []
    for case in CASES:
        for i, turn in enumerate(case["turns"]):
            if turn["expected_behavior"] == "refuse":
                turn["expected_result"] = None
                continue
            columns, rows = runners[case["database"]](turn["reference_sql"])
            turn["expected_result"] = {"columns": columns, "rows": [[_plain(v) for v in row] for row in rows]}
            shown = rows[:2]
            print(f"{case['id']} t{i + 1}: {len(rows)} row(s) {[[_plain(v) for v in r] for r in shown]}")
            if not rows or len(rows) > MAX_ROWS:
                problems.append(f"{case['id']} turn {i + 1}: {len(rows)} rows")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        for case in CASES:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    print(f"\n{len(CASES)} cases -> {out}")
    print("by database:", dict(Counter(c["database"] for c in CASES)))
    print("by category:", dict(Counter(c["category"] for c in CASES)))
    print("grammar unsupported:", sum(not c["grammar_supported"] for c in CASES))
    if problems:
        print("PROBLEMS:", *problems, sep="\n  ")
        sys.exit(1)


if __name__ == "__main__":
    main()
