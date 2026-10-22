# Ablation results (plan step 0d, 2026-10-14)

> Commit references in this document (such as `ff07c84`) point to the development history, which was
> squashed into a single commit for publication. Result files are named in each section.

**Question:** on databases NLviz was never tuned on, does its design answer more questions correctly
than a plain LLM writing SQL with the same context? And do the 13 phrase rules help?

**Answer:** not yet, and no.
- Text-to-SQL, given NLviz's own metric definitions, examples and value hints, answered about three
  times as many first questions correctly as NLviz, with both models.
- NLviz did not make up for that with safety: it gave as many wrong answers or more.
- The phrase rules changed little. Where they changed an outcome, they hurt slightly more often than
  they helped.

## Setup

- Questions: the development set, `backend/eval/bench/`, frozen in `95f0401` before measuring.
  - 120 cases, 139 turns, on three databases: TPC-H (live Postgres), employees (MySQL snapshot) and
    NYC taxi January 2024 (Parquet upload plus an attached zone file).
  - Expected results come from hand-written SQL, run on each database.
  - A policy sheet per database records the business decisions, such as net revenue and current employees.
- Semantic layers: generated once per database by the local model, then frozen. Every approach and
  model used the same three layers.
- Approaches:
  - A: text-to-SQL (`/bench/sql`). Its context is the tables, the joins, every metric and dimension
    as SQL, the value hints, and the retrieved examples compiled to SQL. Its SQL goes through the same
    safety gate and engine, with up to 2 repairs.
  - B: NLviz full.
  - C: NLviz with `PHRASE_REWRITES=off`.
- Models and repeats: qwen3:4b (local) and gpt-4.1-mini, 3 repeats each. The local model gave
  identical repeats; gpt-4.1-mini varied by ±1–2 turns.
- Rewrite replay: the LLM proposals recorded in B, replayed with the rules on and off.
  - It reproduced the recorded outcome for 363 of 417 turns (local) and 373 of 417 (hosted).
  - The rest differ through clarification and repair paths that a replay cannot follow exactly.
- Cost: $1.60 of gpt-4.1-mini for 1,251 hosted turns (about $0.0013 per turn), plus a few cents
  of replay explanations.

## Results

Turns out of 139. First-response outcomes, mean of 3 repeats.

| | Correct answer | Wrong answer | Unnecessary refusal | Correct refusal (of 17) | Error (422) | Final correct |
|---|---|---|---|---|---|---|
| A text-to-SQL, qwen3:4b | **58** | 45 | 17 | 17 | 2 | **75** |
| B NLviz, qwen3:4b | 18 | 49 | 40 | 16 | 13 | 34 |
| C NLviz rules off, qwen3:4b | 18.7 | 48.7 | 40 | 16 | 13.3 | 34.7 |
| A text-to-SQL, gpt-4.1-mini | **63.7** | 44.7 | 13.7 | 17 | 0 | **80.7** |
| B NLviz, gpt-4.1-mini | 22.3 | 61.3 | 23.7 | 17 | 11.7 | 39.3 |
| C NLviz rules off, gpt-4.1-mini | 25.3 | 58.7 | 23.7 | 17 | 11.3 | 42.3 |

**By database** (correct first answers; text-to-SQL vs NLviz full):

| | qwen3:4b | gpt-4.1-mini |
|---|---|---|
| tpch (52 turns) | 23 vs 9 | 28.3 vs 11 |
| employees (48) | 19 vs 1 | 20 vs 3.3 |
| taxi (39) | 16 vs 8 | 15.3 vs 8 |

**By category** (correct first answers; text-to-SQL vs NLviz full):

| | qwen3:4b | gpt-4.1-mini |
|---|---|---|
| ordinary (59) | 31 vs 12 | 36.7 vs 15 |
| paraphrase (13) | 7 vs 1 | 5.3 vs 1 |
| ambiguous (17) | 3 vs 1 | 4.3 vs 2.3 |
| follow-up turns (33) | 17 vs 4 | 17.3 vs 4 |
| unsupported (17): correct refusals | 17 vs 16 | 17 vs 17 |
| shapes the grammar cannot express (13) | 5 vs 0 | 6 vs 0 |

Both approaches refuse the unanswerable questions (17 of 17). Neither handles the policy questions
(the ambiguous category): both mostly answer with a reading that differs from the policy, or refuse.

## Why NLviz lost

Causes were read from the audit trails and the answers. Each is a general defect, not something
specific to these questions.

1. **The generated semantic layer is missing basic measures.** This is the largest cause.
   - The taxi layer has no trip count at all, so "how many trips" was answered as total fare.
   - TPC-H has no count of line items, customers, suppliers or parts; `unique_orders` stood in.
   - The employees layer has 8 metrics, including sums of employee numbers, and nothing for current
     salary or headcount. NLviz scored 1–3 of 48 there.
   - Text-to-SQL fell back on the raw tables.
2. **The answerability check refuses answerable questions.** All 40 of NLviz's unnecessary refusals
   (local model) came from it, and about 24 with gpt-4.1-mini. It judges against the generated layer,
   not the data, and it also misreads: "year", "nations", "region" and "quarter" were reported missing.
3. **Bugs in the intent path:**
   - `filter_named_values` turned "by ship mode" into the filter `l_shipmode = 'SHIP'`;
   - "per quarter in 1998" lost its year;
   - a hire-year filter compared against the two end dates instead of the range.
4. **The grammar is too narrow.** Questions needing MAX, a percentage, several metrics or top N per
   group got 0 of 13 correct. Instead of refusing, NLviz answered most of them as something else.
5. **NLviz answers follow-ups much worse:** 4 of 33 turns correct, against 17 for text-to-SQL.

## The phrase rules

- Replay, local model: the rules changed the final outcome of 15 of 417 turns.
- Replay, gpt-4.1-mini: with the rules on, 67 correct first answers and 118 final correct; with
  them off, 76 and 127.
- **What each rule did** (final correctness, rules on vs off, when the rule fired):

| Rule | qwen3:4b | gpt-4.1-mini |
|---|---|---|
| filter_named_values | fired 9: hurt 6 | fired 9: hurt 9 |
| unasked_breakdowns | fired 24: helped 3 | fired 2: no change |
| narrowing_follow_up | fired 6: helped 3 | — |
| entity_name_breakdowns, drop_redundant_breakdown, measure_swap_follow_up, how_many_counts | no change | no change |
| the other six | did not fire | did not fire |

## Recommendation for the decision gate

1. **Phrase rules: remove them.**
   - Delete `filter_named_values` now: it is measured as harmful.
   - Remove the others too. The two with a positive effect helped 3 turns each, on one model only;
     that is too thin to justify keeping rules that react to wording.
   - The old Olist evals were tuned with these rules and may drop. That is expected, and the change
     should be reported as it comes out.
2. **Model: gpt-4.1-mini for the hosted app,** as already chosen. It adds about 4–6 correct first
   answers per approach at about $0.0013 per turn. The local model stays for development.
3. **Architecture: close the gap, then decide.** Do this in two steps rather than pivot on one experiment.
   - a. Fix the general defects above:
     - a count metric for every entity table;
     - answerability judged against the data's tables and columns, not only the layer;
     - date-range handling;
     - removing the rules.
   - b. Add the text-to-SQL path as a guarded fallback:
     - it covers what the grammar cannot express and what NLviz refuses as unanswerable but the data has;
     - it uses the same context and safety gate;
     - its answers are labelled.
   - Then re-run this benchmark. If NLviz's own path still trails text-to-SQL, make generated SQL the
     main path. The semantic layer then becomes its definitions and policies (Phase 1), and NLviz's
     checks (fan-out, mixed units, the interpretation panel) apply to the SQL.
4. **Phase 1 matters for both approaches.** About a third of text-to-SQL's answers are also wrong,
   mostly on policy:
   - current versus all employees;
   - net versus gross revenue;
   - which payment code means cash.

   Confirmed business definitions are the shared next step whichever path wins.

## After the gap fixes (2026-10-17, local qwen3:4b, one repeat)

The defects above were fixed without waiting for the review, since they are needed whichever way the
gate goes:
- `6c3ef6f`: `filter_named_values` removed.
- `9cb764f`: a count for every table; keys found by uniqueness; no sums of keys or codes; camelCase names split.
- `9aeb7ca`: the answerability check sees each dimension's column and the time grains.
- `53f835c`: dates.
  - A period applies to the metric's own date.
  - Date names are mapped by column.
  - Date columns become time dimensions.
- `9c8371a`: an attached lookup is joined by its unique key; lowercase fused IDs count as IDs.
- `fb7c255`: only a count of the same column covers a row count.
- `1502c50`: row counts are named after what a row is ("trips"); small codes and unique names are breakdowns.

The layers were regenerated for each round. Text-to-SQL got the same new layers as context.

| Round | NLviz correct first answer (tpch / employees / taxi) | NLviz wrong | NLviz unnecessary refusal | NLviz final correct | Text-to-SQL correct first answer | Text-to-SQL final correct |
|---|---|---|---|---|---|---|
| Before (0d) | 18 (9 / 1 / 8) | 49 | 40 | 34 | 58 | 75 |
| Round 1 | 33 (21 / 3 / 9) | 69 | 7 | 49 | 58 | 75 |
| Round 2 (lookup join) | 33 (21 / 4 / 8) | 73 | 10 | 48 | 63 | 80 |
| Round 3 (row-count cover) | 33 (21 / 4 / 8) | 75 | 9 | 48 | 64 | 81 |
| Round 4 (naming, breakdowns) | **46** (21 / 5 / 20) | 60 | 7 | **63** | 65 | 82 |

**Round 5:** `filter_named_values` was restored with a guard (`fd9e326`). Removing it had cost about 9
questions on the older eval sets (run 16), and every harmful firing here was one pattern, a value that
is part of its own column's name ("by ship mode" -> 'SHIP'). With the guard, NLviz gets 47 correct
first answers, 59 wrong and 64 final correct. Run 17 restores the older sets.

**Hosted, after the fixes** (gpt-4.1-mini, one repeat, $0.35; `fix5_gpt-*.json`):

| Approach | Correct first answer (tpch / employees / taxi) | Wrong | Unnecessary refusal | Final correct |
|---|---|---|---|---|
| NLviz before (0d, mean of 3) | 22.3 (11 / 3.3 / 8) | 61.3 | 23.7 | 39.3 |
| NLviz now | **59** (21 / 14 / 24) | 49 | 7 | **76** |
| Text-to-SQL now | 75 (26 / 20 / 29) | 44 | 3 | 92 |

**Hybrid: NLviz with the guarded SQL fallback** (`SQL_FALLBACK=on`, `b83abac`; local model, `fix5_qwen-nlviz-fallback.json`):
- Totals: 51 correct first answers (47 without the fallback), 62 wrong, 5 unnecessary refusals, 0 errors (4 without), 68 final correct.
- The fallback ran on 27 turns:
  - all 16 of the unanswerable questions it took were still refused, so no correct refusal was lost;
  - 4 refusals became correct answers and 2 became wrong answers; 5 stayed refused.
- What it does not fix: the remaining gap to text-to-SQL is NLviz's wrong answers on questions it does try (59–62 against 45), not its refusals.

**After review: fixed measurement and the guarded SQL path** (local model, `fix6_qwen-*.json`):
- Fixes:
  - the text-to-SQL context now sends column names (all were null before, `2cec014`);
  - the scorer matches whole numbers exactly, and scores an answer that skipped a required clarification as `unconfirmed_answer` (`fe2dc27`).
- Arm added: text-to-SQL with the app's pushdown guard (`18a8d96`).

| Approach (local) | Correct first answer (tpch / employees / taxi) | Wrong | Unnecessary refusal | Final correct |
|---|---|---|---|---|
| NLviz | 44 (21 / 6 / 17) | 63 | 7 | 60 |
| Text-to-SQL, unguarded | 62 (24 / 20 / 18) | 51 | 7 | 79 |
| Text-to-SQL, guarded | 62 (25 / 19 / 18) | 51 | 8 | 79 |

- **The guard rejected no query here,** so it costs nothing in accuracy.
- **The stricter scorer took about 3 off each approach;** the gap is unchanged.

**The same three approaches on gpt-4.1-mini** (`fix6_gpt-*.json`, 2026-10-17, $0.48):
- Cost control: the app's own monthly cap (`LLM_MONTHLY_BUDGET_USD=0.50`) did the limiting. It
  stopped the guarded approach partway through its last 4 taxi cases (7 of their 8 turns), so the three are compared on the
  131 turns all of them finished.

| Approach (hosted, 131 shared turns) | Correct first answer (tpch / employees / taxi) | Wrong | Unnecessary refusal | Not understood | Final correct |
|---|---|---|---|---|---|
| NLviz | 45 (21 / 13 / 11) | 54 | 7 | 5 | 62 |
| Text-to-SQL, unguarded | 64 (28 / 23 / 13) | 48 | 2 | 0 | 81 |
| Text-to-SQL, guarded | 64 (28 / 23 / 13) | 47 | 2 | 0 | 82 |

- **On all 139 turns** (NLviz and unguarded only): NLviz 52 correct and 69 final; text-to-SQL 71 and 88.
  Before the measurement fixes these were 59 and 75.
- Guarded vs unguarded: both answered exactly the same turns correctly, and the guard rejected no
  query. The guard is free on both models.
- NLviz alone: answered just one turn correctly that both SQL approaches missed (taxi-15).
- A bug the cap exposed: `/bench/sql` reported the cap as a 500; it now returns 503
  `LLM_BUDGET_EXCEEDED`, as `/chat` does.

## SQL-primary with the semantic protections (2026-10-18)

The decision gate's third condition: the protections are ported to the SQL the model writes
(`app/services/sql_checks.py`, `640d1ff`, `c802832`), and the path runs in the app (`ANSWER_PATH=sql`,
`app/agent/nodes/sql_primary.py`), through `/chat` with clarifications and the interpretation read
from the SQL. Checks:
- Double counting: a SUM, AVG or COUNT of one table across a join that repeats its rows. Predicted
  from the join graph, then confirmed on the data under the query's own conditions.
- Agreed definitions: a metric's condition (`base_filter`) is kept, unless the model says the
  question overrides it.
- Mixed units: one figure across currencies is asked about, probed per group.
- Zero placeholders: an average over zeros that may mean "not recorded" is asked about.

| Approach | Model | Correct first (tpch / employees / taxi) | Wrong | Unnecessary refusal | Final correct |
|---|---|---|---|---|---|
| NLviz (semantic) | local | 44 (21 / 6 / 17) | 63 | 7 | 60 |
| Text-to-SQL, guarded | local | 62 (25 / 19 / 18) | 51 | 8 | 79 |
| **SQL-primary** (`fix8_qwen`) | local | **65** (25 / 22 / 18) | **49** | 8 | **82** |
| NLviz (semantic) | gpt-4.1-mini | 52 (21 / 13 / 18) | 55 | 7 | 69 |
| Text-to-SQL, unguarded | gpt-4.1-mini | 71 (28 / 23 / 20) | 49 | 2 | 88 |
| **SQL-primary** (`fix7_gpt`) | gpt-4.1-mini | **71** (28 / 25 / 18) | 49 | 2 | **88** |

All on 139 turns. On the 131 turns the hosted guarded run finished: SQL-primary 66, guarded 64,
NLviz 45. The hosted run cost $0.15.

- **The protections cost nothing in accuracy and refuse a few known-wrong numbers.** The double-counting
  check fired on 6–7 turns per run:
  - the model repaired most of them (emp-04: salaries counted once per historical title);
  - two employees questions (emp-32, emp-36) were refused rather than answered, because each salary
    really was repeated (every historical title or department joined) and the local model could not
    repair it. They score as unnecessary refusals; without the check they were wrong answers.
- **The data confirmation removed a false positive.** In `fix7_qwen`, without it, "mean salary in each
  department today" (emp-17) was refused, although today's salary and today's department keep one row
  per employee.
- **The other checks did not fire on this set.** No layer here has a unit column or a zeros variant,
  and the models kept the `base_filter` conditions. They are covered by tests (`tests/test_sql_checks.py`).
- **The hosted run is the version before the data confirmation** (`fix7`); locally, that confirmation
  added 1 correct answer (64 → 65).
- Rate limit: the first local run (`fix7_qwen`) was faster than the app's per-user limit, so 14 turns
  were refused with 429. Those cases were re-run and merged (the file says so), and the harness now waits
  on 429.
- **After the first review of the checks** (six defects, fixed in `b69442a`: CTE lineage, `SUM(DISTINCT)`,
  literal case, choices enforced, `NULLIF`, metric expressions), the local re-run (`fix9_qwen`) gave the
  same outcome on every turn: 65 / 49 wrong / 82 final. The stricter checks cost nothing here.
- **As the app default** (`8e2a5fb`, `fix10_qwen-default`, 2026-10-19), with the value check added:
  65 correct / 48 wrong / 82 final. The value check did not fire on this set (the model wrote stored
  values); it covers the Rio case in the browser test.
- **After review round 2** (six more defects fixed, `0249eff`; `fix11_qwen-default`): 65 / 49 / 82.
  One turn moved (emp-34, a refusal became a wrong answer) with no check involved: model variance.
- **After verification** (V1-V5 fixed; `fix12_qwen-default`): 65 / 49 / 82, the same outcome on
  every turn as `fix11`.
- **After verification pass 2** (composite join keys must be complete; `fix13_qwen-default`): 65 / 49 / 82, the
  same outcome on every turn.
- **Remaining wrong answers (49)** are mostly business meaning (net vs gross revenue, current vs
  historical rows) and model choices. That is Phase 1's work: confirmed definitions in the layer, which
  this path already enforces once they exist.

Where things stand after round 4:
- TPC-H and taxi: NLviz is close to text-to-SQL (21 vs 23 and 20 vs 24).
- Employees: the gap remains (5 vs 18). Its causes:
  - an unasked breakdown by department on "hired each year" (4 turns, a model choice);
  - "which X has the most" answered with the full ranking, where the scorer expects one row;
  - current-vs-all-time policy questions, which need Phase 1;
  - MAX, which the grammar cannot express.
- Wrong answers: NLviz still gives more (60 vs 45). The difference is mostly policy (net vs gross
  revenue, current employees) and model choices on the local model.

Result files: `backend/eval/results/bench/fix4_qwen-*.json`. Rounds 1–3 are summarised here only.

## Owner review of the holdout clarifications (2026-10-20, aggregate only)

The author reviewed the flagged SQL-primary clarifications by hand; only aggregate counts are published. The machine-scored totals
above stay as published. This is a separate, partial adjudication.

| Run | Approved useful | Approved unnecessary | Needs discussion | Dependent turns never run (no verdict) |
|---|---|---|---|---|
| Local SQL-primary | 4 | 3 | 0 | 4 |
| Hosted SQL-primary | 1 | 3 | 1 | 2 |

- Reclassified: 4 local and 1 hosted first responses move from "unnecessary" to "useful"
  clarification.
- Unchanged: correct and wrong answer counts and final correctness. A useful clarification is a
  valid first response, not a correct answer.
- No verdict: the blocked follow-ups were never run, so nothing is claimed about them.
- Still open: one hosted item.

## Definition clarifications (2026-10-21, after the freeze; development set only)

The SQL path now asks before applying an unconfirmed metric condition that changes the answer
(`DEFINITION_CLARIFICATIONS=on`, `app/services/sql_checks.py` `_definitions`). The answer holds for
the conversation, or for the connection when the user ticks "remember". Development set
(`fix14_qwen-definitions`): 65 correct / 48 wrong / 82 final, with no definition question asked.
**None of the three benchmark data models has an inferred condition** (`base_filter`): the schema
generator does not propose them, so the feature has nothing to ask about there. It is exercised on the
hand-written Olist layer (tests/test_definitions.py, the live browser test). The holdout results
above were measured before this feature and are unchanged.

**Extension: status questions from the data.** For SUM and AVG of money (revenue, payments, prices),
a status column on the amount's table or a many-to-one parent counts when the data model lists it as
a dimension. It is asked about when the query's rows hold several values and nothing settles which
count. The choices are "Count every status" first, then "Only X" for each real value; no meaning is
assumed from the names.

| Run | Correct first | Wrong first | Unnecessary clarification | Final correct |
|---|---|---|---|---|
| `fix13` (before) | 65 | 49 | 0 | 82 |
| `fix15` (every SUM/AVG) | 61 | 40 | 14 | 82 |
| `fix16` (money only) | 64 | 39 | 11 | 82 |
| `fix17` (after review D1-D7) | 64 | 40 | 11 | 82 |
| `fix18` (after verification V1-V4) | 64 | 39 | 11 | 82 |
| `fix19` (final build, the default: W1, W2 fixed; W3 documented) | 63 | 42 | 11 | 81 |

- Where it asks: all 11 questions are TPC-H revenue or order-price questions. They ask about order
  status (O/F/P) or line status (O/F).
- Why they score "unnecessary": the development policy sheet counts every status. Final
  correctness is unchanged, because the harness answers with the first choice ("Count every status").
- Wrong answers: 9 wrong first answers became questions. Their final answers are still wrong,
  because their errors are other definitions (net vs gross revenue), which this does not ask about.
- **After review** (seven defects fixed): the outcome is unchanged within one turn of model
  variance. The fixes:
  - an answered definition now wins over the model's "the question asked for it" flag, unless the
    question names the values;
  - "every row" or "every status" rejects any restriction on those columns (any operator);
  - a status filter the model adds needs the question to name it;
  - the "excludes no rows" check asks when the condition sits in a join;
  - the panel renders the conditions that ran;
  - concurrent saves keep the newest snapshot.
- **After verification** (four more blockers fixed): neither the question's wording nor the
  model's SQL authorizes a definition. Only the user's answer in this conversation or the owner's
  confirmation does; otherwise the app asks.
  - A question that spells it out ("including canceled orders") also gets one click to confirm.
  - When the model says a new question asks for something other than an earlier answer, the user is
    asked again, with the earlier answer shown.
  - A confirmation is shared with other conversations only once it is saved.
- Cost: in real use a user answers once per connection with "remember". The benchmark starts
  every case in a new conversation, so it asks every time.

## Sealed holdout, local model (2026-10-20, frozen snapshot)

Written independently and never seen during development; run once with a private runner, which
reported only totals. Three databases (two Postgres, one CSV upload), 123 turns, local `qwen3:4b-instruct-8k` for
every task. Both scorers (the repository's and the holdout's own) gave identical totals.

| Arm | Correct first answer | Wrong | Correct refusal | Unnecessary refusal | Unnecessary clarification | Error | Final correct |
|---|---|---|---|---|---|---|---|
| SQL-primary (the app, `/chat`) | 54 (43.9%, 95% CI 35.4–52.7) | 38 (30.9%, 23.4–39.5) | 15 / 15 | 5 | 7 | 4 | 69 (56.1%, 47.3–64.6) |
| Text-to-SQL baseline (`/bench/sql`, no semantic checks) | 52 (42.3%, 33.9–51.1) | 45 (36.6%, 28.6–45.4) | 15 / 15 | 9 | 0 | 2 | 67 (54.5%, 45.7–63.0) |

Wilson intervals; the turns are not independent (follow-ups share conversations).

- **Fewer wrong answers.** SQL-primary gave 7 fewer wrong answers and 2 more correct ones than the same model writing SQL without the checks.
  Its 7 extra clarifications were scored unnecessary (none matched a case's accepted clarifications).
  The intervals overlap: on 123 turns this is a direction, not a proven difference.
- Refusals: both refused all 15 unsupported questions.
- Weakest area: ambiguous questions that need a business policy: 1 correct of 15. Phase 1's
  confirmed definitions are the missing piece.
- **Review pending (runner rule).** 11 SQL-primary turns are flagged `needs_review`: the app asked a
  clarification no machine rule matched, so the runner sent no reply and did not run their dependent
  follow-ups (4 of these show as errors). A person adjudicates them before the result
  counts as final. The totals above are as measured.
- Holdout vs development set: 43.9% correct first answers on the holdout vs 46.8% (65/139) on the
  development set, so the development-set number holds up out of sample.

## Sealed holdout, gpt-4.1-mini (2026-10-20, same freeze)

The same 123 turns and runner (`--allow-hosted`). Every task ran on `gpt-4.1-mini`, on the data models
the local run generated (as in the development-set hosted runs). The spend was $0.26 (the app's ledger
went from $0.646 to $0.905), under the approved $0.40, with the app cap at $1.05. Both scorers gave
identical totals.

| Arm | Correct first answer | Wrong | Correct refusal | Unnecessary refusal | Unnecessary clarification | Error | Final correct |
|---|---|---|---|---|---|---|---|
| SQL-primary (the app, `/chat`) | 58 (47.2%, 38.6–55.9) | 40 (32.5%, 24.9–41.2) | 15 / 15 | 3 | 5 | 2 | 73 (59.3%, 50.5–67.6) |
| Text-to-SQL baseline (`/bench/sql`) | 57 (46.3%, 37.8–55.1) | 46 (37.4%, 29.4–46.2) | 15 / 15 | 0 | 0 | 5 | 72 (58.5%, 49.7–66.9) |

- Same pattern as locally: about the same accuracy with 6 fewer wrong answers. The intervals overlap.
- Ambiguous policy questions: 0 of 15 correct on both arms. Neither model can know an unstated
  business policy; this is Phase 1's work.
- Review pending: 7 SQL-primary turns are flagged `needs_review`, as in the local run.
- Errors: the baseline's 5 errors are request failures the runner recorded; they were not inspected
  (the cases are sealed).
- Model gap: on the holdout, the hosted model added 4 correct answers to SQL-primary (54 → 58). On
  the development set the gap was wider (65 → 71).

## Limits

- **One author.** The development set was written by the same developer as the app, though before it
  was run on these databases. The sealed holdout, written by someone else, is the independent check.
- Small scope: three databases and 139 turns. The differences here are large (about 40 turns of
  139), so they are unlikely to be noise. Small differences, such as rules on or off, are within
  variation.
- **Layers generated by the local model.** gpt-4.1-mini answered on them too. A layer generated by
  the hosted model might be better; that is a separate measurement.
- **Error turns.** NLviz's "error" turns are 422 "question not understood". They count as neither
  correct nor wrong.
