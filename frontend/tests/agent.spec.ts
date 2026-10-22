import type { Page } from "@playwright/test";
import { expect, test } from "./preview-fixture";

/** Phase 3 agent in the browser: streamed steps, follow-up questions, clarification.
 * Runs against the Compose stack with the published "Olist (live Postgres)" connection, on either
 * answer path (ANSWER_PATH=sql, the default, or semantic): the SQL heading and step names differ. */

const CONNECTION = process.env.E2E_CONNECTION ?? "Olist (live Postgres)";

async function openConnection(page: Page) {
  await page.goto("/");
  await page.getByTitle(CONNECTION, { exact: true }).click();
  await expect(page.getByRole("heading", { name: "Ask your data anything." })).toBeVisible();
  // The composer pre-fills an example once the schema loads; typing before that merges the two.
  await expect(page.locator("textarea.textarea")).not.toHaveValue("");
}

async function ask(page: Page, question: string) {
  await page.locator("textarea.textarea").fill(question);
  await page.getByRole("button", { name: /^(Run Query|Ask)$/ }).click();
}

const sql = (page: Page) =>
  page.locator("section", { has: page.getByRole("heading", { name: /^(Compiled|Checked) SQL$/ }) }).locator("pre");

test("a question streams its steps, and a follow-up builds on it", async ({ page }) => {
  await openConnection(page);

  await ask(page, "Show revenue by customer state");
  // Steps arrive while the agent works.
  await expect(page.getByRole("heading", { name: "Working on it" })).toBeVisible();
  // The SQL path asks which definition of revenue to use when nobody has confirmed one; the first
  // choice is the data model's guess. The follow-up below must not ask again (same conversation).
  const definition = page.getByTestId("clarification-choices").getByRole("button").first();
  await expect(definition.or(sql(page))).toBeVisible();
  if (await definition.isVisible()) await definition.click();
  await expect(page.getByText(/^(Understanding the question|Writing and checking the query)$/).first()).toBeVisible();
  await expect(sql(page)).toContainText("customer_state");
  const panel = page.locator("section", { has: page.getByRole("heading", { name: "How it was answered" }) });
  await expect(panel.getByText(/^(Running on Jammi|Writing and checking the query)$/).first()).toBeVisible();
  await expect(panel.getByText(/\d+ ms/).first()).toBeVisible();

  await ask(page, "only for credit card payments");
  await expect(sql(page)).toContainText("'credit_card'");
  await expect(sql(page)).toContainText("customer_state"); // kept from the first question
  const thread = page.locator("section", { has: page.getByRole("heading", { name: "Conversation" }) });
  await expect(thread.getByRole("button")).toHaveCount(2);

  // Earlier turns stay viewable.
  await thread.getByRole("button").first().click();
  await expect(sql(page)).not.toContainText("credit_card");

  // A new conversation does not build on the old one.
  await page.getByRole("button", { name: "New conversation" }).click();
  await expect(thread).toHaveCount(0);
});

test("an ambiguous place asks which one was meant, then answers with the choice", async ({ page }) => {
  await openConnection(page);

  await ask(page, "Orders from customers in Rio");
  // The compiler path asks about the state (RJ, RN, ...); the SQL path about the value it wrote,
  // checked on the data (a state, or a city such as "rio de janeiro").
  await expect(page.getByRole("heading", { name: /Which customer (state|city) did you mean\?/ })).toBeVisible();
  const choices = page.getByTestId("clarification-choices").getByRole("button");
  const fallback = page.getByRole("button", { name: "RJ", exact: true });
  const choice = (await choices.count()) > 0 ? choices.first() : fallback;
  const chosen = (await choice.innerText()).trim();
  await choice.click();
  await expect(sql(page)).toContainText(`'${chosen}'`);
  await expect(page.getByText(new RegExp(`You chose ${chosen}`))).toBeVisible();
});

test("the built-in sample data can be queried without any connection", async ({ page }) => {
  // A fresh install has no connections: the picker offers the bundled sample. Here the picker is
  // reached through "New Connection".
  await page.goto("/");
  await page.getByRole("button", { name: "New Connection" }).click();
  await page.getByRole("button", { name: /try the sample data/i }).click();
  await expect(page.getByText("Sample data: Olist e-commerce")).toBeVisible();
  await expect(page.locator("textarea.textarea")).not.toHaveValue("");
  await ask(page, "How many orders are there?");
  await expect(page.getByRole("heading", { name: "Interpretation" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "99441" })).toBeVisible();
});
