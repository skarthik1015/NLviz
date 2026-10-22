/* Run manually after browser validation: node tests/capture-demo.cjs
 * Records real local-model responses. No API mocking; optional UI preview proxy only.
 */
const { chromium } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

const assets = path.resolve(__dirname, '../../docs/packaging/assets');
const base = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000';
const preview = process.env.E2E_FRONTEND_PREVIEW_URL;

async function main() {
  // Read only non-secret settings from /health/details is not sufficient to establish every
  // provider override. The operator must first inspect the backend's whitelisted task settings.
  fs.mkdirSync(assets, { recursive: true });
  const provenance = JSON.parse(fs.readFileSync(path.join(assets, 'backend-fingerprint.json'), 'utf8'));
  const settings = provenance.model_preflight;
  if (settings.LLM_PROVIDER !== 'openai' || settings.OPENAI_BASE_URL !== 'http://host.docker.internal:11434/v1' ||
      !settings.LLM_MODEL?.startsWith('qwen3:') ||
      settings.DEFINITION_CLARIFICATIONS !== 'on' ||
      Object.entries(settings).some(([key, value]) => /^LLM_(MODEL|PROVIDER)_/.test(key) && value)) {
    throw new Error('Capture requires verified local-only settings in backend-fingerprint.json');
  }
  const head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: path.resolve(__dirname, '../..'), encoding: 'utf8' }).trim();
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 },
    recordVideo: { dir: assets, size: { width: 1440, height: 1080 } } });
  const page = await context.newPage();
  if (preview) {
    await page.route(`${new URL(base).origin}/**`, async route => {
      const original = new URL(route.request().url());
      const response = await route.fetch({ url: new URL(original.pathname + original.search, preview).toString() });
      await route.fulfill({ response });
    });
  }
  const observations = [];
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  async function open(name) {
    await page.goto(base);
    await page.getByTitle(name, { exact: true }).click();
    await page.locator('textarea.textarea').waitFor();
    await page.waitForFunction(() => document.querySelector('textarea.textarea')?.value.length > 0);
    // Keep leftover development connection names out of the presentation using the real UI.
    // Selecting a connection still happens through the existing sidebar before collapsing it.
    const collapse = page.getByTitle('Collapse sidebar', { exact: true });
    if (await collapse.isVisible()) await collapse.click();
    await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().width <= 60);
  }
  async function submit(question) {
    await page.locator('textarea.textarea').fill(question);
    await page.getByRole('button', { name: /^(Run Query|Ask)$/ }).click();
  }
  async function completed() {
    await page.getByRole('heading', { name: 'Working on it' }).waitFor({ state: 'hidden', timeout: 120000 });
  }
  async function shot(name) {
    await page.screenshot({ path: path.join(assets, name), fullPage: true });
  }
  let error;
  try {
    await open('Olist (live Postgres)');
    await submit('What is the total revenue?');
    await completed();
    let definitionChoices = page.getByTestId('clarification-choices');
    await definitionChoices.waitFor();
    await shot('definition-question.png');
    const definitionPrompt = await definitionChoices.innerText();
    await definitionChoices.getByRole('button', { name: /Leave out cancell?ed, unavailable/ }).click();
    await completed();
    await page.getByRole('heading', { name: 'Checked SQL', exact: true }).waitFor();
    await page.getByRole('region', { name: 'Interpretation', exact: true }).waitFor();
    await shot('definition-filtered-answer.png');
    observations.push({ flow: 'definition: exclude cancelled/unavailable', question: 'What is the total revenue?',
      choices: definitionPrompt, interpretation: await page.getByRole('region', { name: 'Interpretation', exact: true }).innerText(),
      remember: false });

    await open('Olist (live Postgres)');
    await submit('What is the total revenue?');
    await completed();
    definitionChoices = page.getByTestId('clarification-choices');
    await definitionChoices.waitFor();
    await definitionChoices.getByRole('button', { name: 'Count every row (no condition)', exact: true }).click();
    await completed();
    await page.getByRole('heading', { name: 'Checked SQL', exact: true }).waitFor();
    await page.getByRole('region', { name: 'Interpretation', exact: true }).waitFor();
    await shot('definition-all-rows-answer.png');
    observations.push({ flow: 'definition: all rows', question: 'What is the total revenue?',
      interpretation: await page.getByRole('region', { name: 'Interpretation', exact: true }).innerText(),
      remember: false });

    // This follow-up keeps the all-rows choice in this conversation; nothing is saved connection-wide.
    await submit('Revenue by state, top 5');
    await page.getByRole('heading', { name: 'Working on it' }).waitFor();
    await shot('query-in-progress.png');
    await completed();
    await page.getByRole('heading', { name: 'Checked SQL', exact: true }).waitFor();
    await page.getByRole('region', { name: 'Interpretation', exact: true }).waitFor();
    // The response can finish before the dynamically imported Plotly bundle is drawn.
    await page.locator('.chart-canvas .main-svg').first().waitFor({ timeout: 60000 });
    await page.getByText('SQL written by the model, checked against the data model', { exact: true }).waitFor();
    const chartTitle = await page.locator('.chart-canvas .gtitle').first().textContent();
    if (!chartTitle?.trim() || /^metric value$/i.test(chartTitle.trim())) {
      throw new Error('Expected the measure title from the post-freeze presentation fix');
    }
    await shot('answer-chart-interpretation.png');
    observations.push({ flow: 'answer', question: 'Revenue by state, top 5', checked_sql: true,
      chart_title: chartTitle, answered_by: 'SQL written by the model, checked against the data model' });

    await page.setViewportSize({ width: 390, height: 844 });
    // The running build may retain the expanded desktop sidebar when the viewport shrinks.
    // Use the real navigation control, rather than hide it or alter styles for the screenshot.
    const collapse = page.getByTitle('Collapse sidebar', { exact: true });
    if (await collapse.isVisible()) await collapse.click();
    await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().width <= 60);
    await page.getByRole('heading', { name: 'Ask your data anything.' }).scrollIntoViewIfNeeded();
    const mobile = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth,
      composer: document.querySelector('.composer').getBoundingClientRect().toJSON() }));
    if (mobile.composer.right > mobile.viewport + 1 || mobile.document > mobile.viewport + 1 || mobile.composer.width < 250) {
      throw new Error('Phone layout overflows viewport: ' + JSON.stringify(mobile));
    }
    await shot('mobile-answer.png');
    observations.push({ flow: 'mobile', sidebar: 'collapsed using existing UI control', ...mobile });
    await page.setViewportSize({ width: 1440, height: 1080 });

    await open('Zomato Restaurant Dataset');
    await submit('average cost for two');
    await completed();
    const choices = page.getByTestId('clarification-choices');
    await choices.waitFor({ timeout: 10000 });
    const clarification = await choices.innerText();
    if (!/currency/i.test(await page.locator('.workspace').innerText())) {
      throw new Error('Expected a currency clarification, not another answer state');
    }
    await shot('currency-clarification.png');
    observations.push({ flow: 'currency clarification', question: 'average cost for two', choices: clarification });

    await open('Olist (live Postgres)');
    await submit('Will it rain tomorrow?');
    await completed();
    await page.getByTestId('unsupported').waitFor();
    await shot('unsupported.png');
    observations.push({ flow: 'refusal', question: 'Will it rain tomorrow?',
      explanation: await page.getByTestId('unsupported').innerText() });
    if (errors.length) throw new Error('Browser runtime error: ' + errors.join('; '));
  } catch (caught) {
    error = caught;
    await shot('capture-incomplete.png');
  } finally {
    const video = page.video();
    await context.close();
    await video.saveAs(path.join(assets, error ? 'demo-incomplete.webm' : 'demo.webm'));
    await video.delete();
    await browser.close();
    const files = fs.readdirSync(assets).filter(name => /\.(png|webm)$/.test(name));
    fs.writeFileSync(path.join(assets, 'capture.json'), JSON.stringify({
      captured_at: new Date().toISOString(), frontend_repository_head: head,
      frontend: preview ? 'review worktree preview' : 'running Compose frontend (image in backend-fingerprint.json)',
      backend: 'running Compose image; source/image fingerprints are recorded in backend-fingerprint.json',
      model: settings.LLM_MODEL, answer_path: settings.ANSWER_PATH,
      model_preflight: 'backend-fingerprint.json; all task overrides checked before capture',
      browser: 'Chromium / Playwright', preview: preview || null, api_mocked: false,
      sidebar: 'collapsed using existing UI control after every connection selection; connections not deleted',
      complete: !error, observations, errors,
      artifacts: files.map(name => ({ name, sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(assets, name))).digest('hex') })),
    }, null, 2) + '\n');
  }
  if (error) throw error;
  console.log('Captured both definition replies, answer, progress, mobile, currency clarification and refusal from the local stack.');
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
