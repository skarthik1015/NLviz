import { expect, test, type Page } from "@playwright/test";
import type { ChatResponse, Interpretation } from "../app/lib/types";

const interpretation: Interpretation = {
  path: "semantic",
  metric: { name: "hires", label: "Hires", aggregation: "count", definition: "COUNT(employees.employee_id)", description: "Employees hired in the selected period." },
  filters: [
    { dimension: "department", label: "Department", operator: "in", value: ["Sales", "Support"] },
    { dimension: "exit_date", label: "Exit date", operator: "is_null", value: null },
  ],
  thresholds: ["Hires > 5"],
  grouping: [{ name: "department", label: "Department" }],
  time_grain: { name: "month", label: "Month" },
  period: { column: "employees.hire_date", label: "Hire Date", start: "1990-01-01", end: "1990-12-31" },
  order_by: "Hires descending", limit: 10,
  policies: [
    { name: "active", statement: "Include active employees.", source: "inferred" },
    { name: "contractors", statement: "Exclude contractors.", source: "confirmed" },
    { name: "timezone", statement: "Timezone has not been specified.", source: "unknown" },
  ],
  notes: ["Dates include both endpoints."],
};

function answer(overrides: Partial<ChatResponse> = {}): ChatResponse {
  return {
    query_id: "mock-answer", question: "Show hires by department in 1990",
    intent: { metric: "hires", dimensions: ["department"], filters: [], time_dimension: "hire_date", time_granularity: "month", start_date: "1990-01-01", end_date: "1990-12-31", order_by: "metric_desc", limit: 10 },
    intent_source: "llm", status: "answered", conversation_id: "mock-conversation", clarification: null,
    sql: "SELECT department, COUNT(employee_id) FROM employees GROUP BY department",
    rows: [{ department: "Sales", hires: 12 }], row_count: 1, trace: [], validation_status: "ok", chart_spec: null,
    explanation: "Sales had 12 hires.", debug_trace: null, evidence: [], interpretation,
    ...overrides,
  };
}

test.beforeEach(async ({ page }) => {
  // Catch all API calls so this suite cannot contact an agent, database or LLM.
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/connections")) {
      await route.fulfill({ json: [{ connection_id: "mock", display_name: "Mock employees", connector_type: "postgres", created_at: "2026-01-01", owner_id: "mock", status: "active", query_ready: true }] });
    } else if (path.endsWith("/schema")) {
      await route.fulfill({ json: { connector_type: "postgres", tables: {}, row_counts: {}, join_paths: [], metrics: [{ name: "hires", display_name: "Hires" }], dimensions: [], time_dimensions: [], suggested_questions: ["Show hires by department in 1990"] } });
    } else if (path.endsWith("/auth/me")) {
      await route.fulfill({ json: { user_id: "mock", email: null } });
    } else {
      await route.fulfill({ status: 500, json: { detail: "Unexpected API request in offline test" } });
    }
  });
});

async function openAndAsk(page: Page) {
  await page.goto("/");
  await expect(page.locator("textarea.textarea")).not.toHaveValue("");
  await ask(page);
}

async function ask(page: Page) {
  await page.locator("textarea.textarea").fill("Show hires by department in 1990");
  await page.getByRole("button", { name: /^(Run Query|Ask)$/ }).click();
}

async function mockAnswer(page: Page, response: ChatResponse) {
  await page.route("**/api/chat/stream", (route) => route.fulfill({
    contentType: "text/event-stream", body: `event: result\ndata: ${JSON.stringify(response)}\n\n`,
  }));
}

test("an answered question shows its read-only interpretation and preserves it in history", async ({ page }) => {
  await mockAnswer(page, answer());
  await openAndAsk(page);
  const panel = page.getByRole("region", { name: "Interpretation", exact: true });
  await expect(panel).toBeVisible();
  for (const text of ["Hires (count)", "COUNT(employees.employee_id)", "Employees hired in the selected period.", "Department in Sales, Support; Exit date is empty", "Hires > 5", "Month", "Hire Date (employees.hire_date), 1990-01-01 to 1990-12-31", "Hires descending", "10", "Dates include both endpoints.", "inferred", "confirmed", "unknown"]) {
    await expect(panel.getByText(text, { exact: true })).toBeVisible();
  }
  await expect(panel.locator("input, textarea, select, button")).toHaveCount(0);
  await expect(page.getByRole("cell", { name: "Sales", exact: true })).toBeVisible();

  await mockAnswer(page, answer({ query_id: "generated", interpretation: { ...interpretation, path: "generated_sql", metric: null, policies: [], period: null }, sql: "SELECT 12 AS hires" }));
  await ask(page);
  await expect(panel.getByText("Generated SQL, not checked against definitions", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Generated SQL", exact: true })).toBeVisible();
  await expect(panel.getByText("COUNT(employees.employee_id)")).toHaveCount(0);
  const thread = page.locator("section", { has: page.getByRole("heading", { name: "Conversation", exact: true }) });
  await thread.getByRole("button").first().click();
  await expect(panel.getByText("COUNT(employees.employee_id)", { exact: true })).toBeVisible();
  await expect(panel.getByText("Generated SQL, not checked against definitions")).toHaveCount(0);
});

test("unsupported is an answer state with missing data and an alternative", async ({ page }) => {
  const explanation = "This data has no salary history. You can ask for hires by department instead.";
  await mockAnswer(page, answer({ status: "unsupported", explanation, interpretation: null, rows: [], row_count: 0, sql: null }));
  await openAndAsk(page);
  await expect(page.getByTestId("unsupported")).toContainText(explanation);
  await expect(page.locator(".error")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Tabular Result" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Interpretation", exact: true })).toHaveCount(0);
});

for (const transport of ["http", "stream"] as const) {
  test(`QUESTION_NOT_UNDERSTOOD gives rephrasing guidance over ${transport}`, async ({ page }) => {
    const detail = "QUESTION_NOT_UNDERSTOOD: internal mapper detail";
    await page.route("**/api/chat/stream", (route) => route.fulfill(transport === "http"
      ? { status: 422, json: { detail } }
      : { contentType: "text/event-stream", body: `event: error\ndata: ${JSON.stringify({ status: 422, detail })}\n\n` }));
    await openAndAsk(page);
    await expect(page.locator(".error")).toHaveText("I couldn't interpret that; try rephrasing.");
    await expect(page.getByText("internal mapper detail")).toHaveCount(0);
  });
}
