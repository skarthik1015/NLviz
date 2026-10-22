# Benchmark 

The development set: 120 cases on three databases NLviz was never tuned on, written before the app
was run on them. It guides development, so it stops being unseen evidence once its failures shape
changes; the final evaluation uses a sealed holdout written by someone else (kept outside this repository).

| File | What |
|---|---|
| `cases_dev.py` | The cases, with hand-written reference SQL and meaning |
| `policies_dev.md` | The business decisions the expected results follow |
| `build_cases.py` | Runs every reference query on its database and writes `cases/dev.jsonl` |
| `cases/dev.jsonl` | The frozen case file (change only in a commit of its own, before measuring) |
| `score.py` | Compares results by meaning (whole numbers exact); seven first-response outcomes plus final correctness |
| `run.py` | Sets up the connections once, then asks the cases through the API |
| `datasets/` | Loaders: TPC-H (Postgres), employees (MySQL), NYC taxi (files to upload) |

Databases: `tpch` (TPC-H scale 0.1, live Postgres), `employees` (datacharmer/test_db, MySQL
snapshot), `taxi` (NYC TLC yellow taxi January 2024 Parquet + zone lookup CSV, upload + attached file).

Mix: 120 cases, 45 / 40 / 35 per database; 59 ordinary (13 of them shapes the grammar cannot express
yet), 13 paraphrases, 17 ambiguous, 17 unsupported, 14 follow-up conversations (33 turns).

Outcomes of the first response: correct_answer, wrong_answer, correct_refusal, unnecessary_refusal,
useful_clarification, unnecessary_clarification, unconfirmed_answer (a question that needs a
clarification answered without one, matching the policy by chance), plus `error` for an HTTP failure. Final
correctness is scored after the scripted clarification answers. A clarification no machine rule
matches, on a case listing one without a rule, is flagged `needs_review` for a person.
