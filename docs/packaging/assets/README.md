# Demo capture — 2026-10-20 (two images refreshed 2026-10-21)

**Refreshed 2026-10-21:** `answer-chart-interpretation.png` and `definition-question.png` were
recaptured with a plain Playwright script, from the current build (definition fixes through
W1/W2), local qwen3:4b, after the 18 benchmark connections were removed from the sidebar. The question
was "Revenue by customer state, top 5" on Olist, answered with the data model's guess. The earlier
answer image showed a since-fixed panel defect (D6). These two images are not covered by the capture
fingerprints below; the other images are the 2026-10-20 capture.


These are real browser responses from the current default Compose frontend and local Qwen
SQL-primary backend, source base `fa808ea`, including post-freeze definition changes `c3d0aa8` and
`7b32a2f`. This is a demonstration of the ordinary flow; the definition feature's
review findings, fixed afterwards.
The chart is titled **Total Revenue** and Query Metadata says **SQL written by the model, checked
against the data model**; the capture asserts both. This build is later than the scored `v2.0-rc`
freeze, and no holdout was rerun for these assets. The review gate closed separately through offline
verification for the earlier feature set; that does not approve the new definition feature.
Screenshots are a demonstration, not independent correctness evidence.
Backend source checksums and image IDs are in [backend-fingerprint.json](backend-fingerprint.json).
[capture.json](capture.json) records model settings, capture time, browser observations and hashes.

- [Definition question](definition-question.png): Olist “What is the total revenue?” asks whether
  to exclude cancelled/unavailable orders or count every row.
- [Filtered reply](definition-filtered-answer.png) and [all-row reply](definition-all-rows-answer.png):
  the two answers from separate conversations, with the actual calculation and policy source.
  The “remember” checkbox was left off in both; no connection-wide definition was saved. The
  grouped answer below follows the all-row choice in its conversation.

The grouped follow-up visibly retains the data model's “non-cancelled orders” description despite
running without a status filter, and attributes the override to the question. This is a recorded
review defect (D1/D6), not accurate provenance. The capture preserves the actual UI; it does not
present a repaired or fabricated answer. The two initial scalar replies show their chosen policies.

- [Answer, chart and interpretation](answer-chart-interpretation.png): “Revenue by state, top 5” on
  the existing Olist development connection. Capture waits for Plotly to draw, not only API completion.
- [Query in progress](query-in-progress.png): the UI while the local model is working. The SQL-primary
  graph streams a completed node update, not partial SQL tokens or each internal validation check.
- [Phone layout](mobile-answer.png): the same response at phone width, after collapsing the sidebar
  with its existing control. The expanded sidebar leaves too little room in this build. Composer
  width/fit was checked; wide data tables and SQL scroll inside their own containers.
- [Currency clarification](currency-clarification.png): “average cost for two” on the existing Zomato
  upload. The screenshot shows the question and choices; enforcement is covered by the regression
  suite rather than inferred from the presence of a button.
- [Unsupported question](unsupported.png): “Will it rain tomorrow?” on Olist.
- [Short recording](demo.webm): those flows, including a phone-width inspection. WebM is used instead
  of GIF to retain legible text without a large palette animation.

Existing development connections were retained. The real sidebar control collapses navigation
after every connection selection, on desktop and phone; no connection was deleted. The recording
briefly expands navigation to select the named development connections. No holdout connection or
case was opened.
The currency label with replacement characters reflects the stored source text and is not corrected
or invented for the recording. No API responses were mocked and no frontend proxy was used. The
browser used the running Compose UI and API directly. Images were visually inspected after capture.

## Reproduce after final validation

First verify the backend's default path and **all** task model/provider overrides are local, then
refresh `backend-fingerprint.json` from the whitelisted settings and runtime source/image hashes.
The capture script checks that recorded preflight; it does not independently read Docker settings.
From `frontend`, against the current Compose UI:

```powershell
Remove-Item Env:E2E_FRONTEND_PREVIEW_URL -ErrorAction SilentlyContinue
npx.cmd playwright test --config playwright.config.ts
node tests/capture-demo.cjs
```

For a worktree preview, the optional `E2E_FRONTEND_PREVIEW_URL` can target its UI on port 3108 while
retaining the normal browser origin. That mode was not used for this capture.
The named Olist and Zomato connections must already be published. The capture script stops with an
incomplete recording if the expected real answer states are absent. It never creates a fake answer
to make a screenshot pass. Recapture if application or cosmetic changes alter these flows.
