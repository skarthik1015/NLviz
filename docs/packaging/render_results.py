"""Render development and holdout results from canonical Markdown; never run an evaluation."""
from pathlib import Path
import hashlib
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'docs/ablation-results.md'


def holdout_results(source):
    sections = re.findall(r'^## Sealed holdout, ([^\n]+)\n(.*?)(?=^## |\Z)', source, re.M | re.S)
    if len(sections) != 2:
        raise SystemExit('Expected two published holdout sections; review parser before publishing.')
    rows = []
    pending = []
    for title, section in sections:
        turns = re.search(r'\b(\d+) turns\b', section)
        model = re.search(r'local `([^`]+)` for', section) if title.startswith('local model') else re.search(r'Every task ran on `([^`]+)`', section)
        review = re.search(r'(\d+) SQL-primary turns are flagged', section)
        table = [line for line in section.splitlines() if line.startswith('| SQL-primary') or line.startswith('| Text-to-SQL baseline')]
        if not turns or not model or not review or len(table) != 2 or 'identical totals' not in section:
            raise SystemExit('Holdout source format changed; cannot safely publish inferred totals.')
        pending.append(f'{model.group(1)}: {review.group(1)} SQL-primary turns flagged `needs_review`')
        for line in table:
            cells = [cell.strip() for cell in line.strip('|').split('|')]
            if len(cells) != 8:
                raise SystemExit('Holdout outcome columns changed; review parser.')
            rows.append('| ' + ' | '.join([cells[0], f'`{model.group(1)}`', 'freeze 2026-10-20', turns.group(1),
                                           'Repository + sealed (identical reported totals)', *cells[1:]]) + ' |')
    return '''# Sealed holdout (2026-10-20)

| Arm | Model | Freeze tag | Turns | Scorers | Correct first answer | Wrong | Correct refusal | Unnecessary refusal | Unnecessary clarification | Error | Final correct |
|---|---|---|---|---|---|---|---|---|---|---|---|
''' + '\n'.join(rows) + '''

**Review pending:** ''' + '; '.join(pending) + '''. These are the published, as-measured totals,
not owner-adjudicated results. Unmatched clarifications received no scripted reply; dependent
follow-ups were not executed. Both scorers agree on the recorded outcomes, which does not settle
whether the unmatched clarifications were useful. No totals were changed for this page.

The two scorers are the repository strict scorer and the frozen holdout scoring contract, as
reported in the canonical source. A shared table records their identical totals; it does not
represent two independent evaluation runs. See [freeze-manifest.md](../freeze-manifest.md) for
the frozen application and runner provenance. Later presentation changes are not scored here.
Hosted answers used the same locally generated semantic layers. Wilson intervals are copied from
the source; turns within conversations are related. Overlapping intervals do not establish a
significant advantage for either arm. The holdout is three databases, not a generalization claim
across arbitrary databases. Cases and owner review material remain private, outside the repository.
'''


def owner_review(source):
    section = re.search(r'^## Owner review of the holdout clarifications[^\n]*\n(.*?)(?=^## |\Z)', source, re.M | re.S)
    if not section:
        raise SystemExit('Owner aggregate section missing; review parser before publishing.')
    rows = [line for line in section.group(1).splitlines()
            if line.startswith(('| Local SQL-primary', '| Hosted SQL-primary'))]
    if len(rows) != 2 or any(len(line.strip('|').split('|')) != 5 for line in rows):
        raise SystemExit('Owner aggregate table changed; review parser before publishing.')
    counts = [[int(cell.strip()) for cell in line.strip('|').split('|')[1:]] for line in rows]
    outstanding = sum(row[2] for row in counts)
    status = 'partial' if outstanding else 'complete'
    return f'''## Separate owner adjudication ({status})

| Run | Approved useful | Approved unnecessary | Needs discussion | Dependent turns never run (no verdict) |
|---|---|---|---|---|
''' + '\n'.join(rows) + '''

These owner-approved classifications do not replace the machine-scored table above.
''' + f'''Approved useful: {counts[0][0]} local and {counts[1][0]} hosted. Correct/wrong answer counts and final
correctness are unchanged; no answer is inferred for blocked follow-ups. Items needing discussion:
**{outstanding}**. This table is copied from the public aggregate section; no private review files are read.
'''


def main():
    source = SOURCE.read_text(encoding='utf-8')
    latest = re.search(r'\*\*As the app default\*\* \(`([0-9a-f]+)`, `([^`]+)`, ([\d-]+)\).*?\n\s*(\d+) correct / (\d+) wrong / (\d+) final\.', source, re.S)
    denominator = re.search(r'120 cases, (\d+) turns', source)
    if latest is None or denominator is None:
        raise SystemExit('Canonical results format changed; review the parser before publishing.')
    commit, artifact, date, correct, wrong, final = latest.groups()
    # Follow later default-path verification rounds, not only the original "As the app default" row.
    later = list(re.finditer(r'^- \*\*After[^\n]*`(fix\d+_qwen-default)`[^\n]*:\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)',
                             source, re.M))
    definitions = list(re.finditer(r'^\| `(fix\d+)` \([^|]*default[^|]*\) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|', source, re.M))
    definition_clarifications = None
    if definitions and (not later or definitions[-1].start() > later[-1].start()):
        label, correct, wrong, definition_clarifications, final = definitions[-1].groups()
        artifact = label + '_qwen-definitions'
    elif later:
        artifact, correct, wrong, final = later[-1].groups()
    if definitions or later:
        artifact_path = f'backend/eval/results/bench/{artifact}.json'
        record = subprocess.run(['git', 'log', '-1', '--format=%H %cs', '--', artifact_path],
                                cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip().split()
        if len(record) != 2:
            raise SystemExit('Latest default-path result has no tracked artifact; cannot label its provenance.')
        commit, date = record
    scorer = ROOT / 'backend/eval/bench/score.py'
    source_commit = subprocess.run(['git', 'log', '-1', '--format=%H', '--', 'docs/ablation-results.md'],
                                   cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    definition_note = ''
    if definition_clarifications:
        definition_note = f'''Definition clarifications enabled; {definition_clarifications} first responses scored unnecessary
clarifications under the development policy sheet. This is a post-freeze result. Fewer wrong first
answers alone do not establish improved final accuracy; compare final correctness in the source.
Definition enforcement: the review blockers are fixed; one known issue (a rare save race) is documented in the README.'''
    body = f'''<!-- Generated by render_results.py. Do not edit numbers here. -->
# Latest default-path development result

| Arm | Model | Application commit | Turns | Correct first answer | Wrong first answer | Final correct |
|---|---|---|---|---|---|---|
| SQL-primary (`/chat`) | local Qwen (configured as `qwen3:4b-instruct-8k`) | `{commit}` | {denominator.group(1)} | {correct} | {wrong} | {final} |

Recorded {date}; source artifact label `{artifact}`. The commit is the code/result record's
introduction commit, not a runtime Git attestation. These are selected counts, not a complete
first-response outcome distribution. Final correctness includes the scripted clarification path.
Definitions were generated/inferred. See the source for which checks fired in each experiment.
{definition_note}

**Limitation:** {wrong} wrong first answers among {denominator.group(1)} development turns.
This is a local portfolio demo; the development set has guided implementation changes.
Turns within a conversation are related, not independent trials.

Source: [ablation-results.md](../ablation-results.md), source-document commit `{source_commit}`.
Source-document SHA-256: `{hashlib.sha256(SOURCE.read_bytes()).hexdigest()}`.
Scorer: strict-count/required-clarification repository scorer (`fe2dc27` lineage), current source
SHA-256 `{hashlib.sha256(scorer.read_bytes()).hexdigest()}`. This fingerprint records the scorer
available at generation time, not independent proof of the historical run's loaded scorer.
For historical execution provenance, consult the run metadata and freeze manifest.

Earlier compiler-path, baseline and guarded-baseline measurements remain in the canonical source;
their model, code and scoring differences must not be collapsed into this row.

{holdout_results(source)}
{owner_review(source)}
'''
    (Path(__file__).parent / 'results.generated.md').write_bytes((body.rstrip() + '\n').encode('utf-8'))


if __name__ == '__main__':
    main()
