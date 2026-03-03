"use client";

import { useState } from "react";

import { sendChatQuestion } from "../lib/api";
import { buildPlotSpec } from "../lib/plot-spec";
import { ChatResponse, PlotSpec } from "../lib/types";
import { PlotPreview } from "./plot-preview";
import { ResultsTable } from "./results-table";

const EXAMPLES = [
  "Show total revenue by customer state",
  "Show order count by product category",
  "Show total revenue over time",
];

export function ChatWorkbench() {
  const [question, setQuestion] = useState(EXAMPLES[0]);
  const [result, setResult] = useState<ChatResponse | null>(null);
  const [plotSpec, setPlotSpec] = useState<PlotSpec | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(formData: FormData) {
    const nextQuestion = String(formData.get("question") ?? "").trim();
    if (!nextQuestion) {
      setError("Enter a question before submitting.");
      return;
    }

    setQuestion(nextQuestion);
    setError(null);
    setIsSubmitting(true);

    try {
      const response = await sendChatQuestion(nextQuestion);
      setResult(response);
      setPlotSpec(buildPlotSpec(response));
    } catch (submissionError) {
      setResult(null);
      setPlotSpec(null);
      setError(
        submissionError instanceof Error
          ? submissionError.message
          : "The request failed before the backend returned a response.",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="workspace">
      <section className="panel composer">
        <form action={handleSubmit} className="composer-grid">
          <label className="label">
            Ask a business question
            <textarea
              className="textarea"
              name="question"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="For example: Show average order value by payment type"
            />
          </label>
          <div className="pill-row">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                className="pill"
                type="button"
                onClick={() => setQuestion(example)}
                disabled={isSubmitting}
              >
                {example}
              </button>
            ))}
          </div>
          <div className="toolbar">
            <div className="status">
              {isSubmitting ? "Running semantic query pipeline..." : "Ready to query the backend API."}
            </div>
            <button className="button" type="submit" disabled={isSubmitting}>
              {isSubmitting ? "Querying..." : "Run Query"}
            </button>
          </div>
          {error ? <div className="error">{error}</div> : null}
        </form>
      </section>

      {result ? (
        <div className="results">
          <div className="stack">
            <section className="panel section">
              <h2 className="section-title">Tabular Result</h2>
              <ResultsTable rows={result.rows} />
            </section>

            <section className="panel section">
              <h2 className="section-title">Compiled SQL</h2>
              <pre className="code">{result.sql}</pre>
            </section>
          </div>

          <div className="stack">
            <section className="panel section">
              <h2 className="section-title">Plot Preview</h2>
              {plotSpec ? <PlotPreview spec={plotSpec} /> : <div className="empty">No plot available yet.</div>}
            </section>

            <section className="panel section">
              <h2 className="section-title">Plot Spec</h2>
              <pre className="code">{plotSpec ? JSON.stringify(plotSpec, null, 2) : "{}"}</pre>
            </section>

            <section className="panel section">
              <h2 className="section-title">Execution Trace</h2>
              <div className="trace">
                {result.trace.map((item) => (
                  <div className="trace-item" key={item}>
                    {item}
                  </div>
                ))}
              </div>
            </section>

            <section className="panel section">
              <h2 className="section-title">Query Metadata</h2>
              <div className="meta-grid">
                <div className="meta-row">
                  <span className="meta-key">Query ID</span>
                  <span>{result.query_id}</span>
                </div>
                <div className="meta-row">
                  <span className="meta-key">Metric</span>
                  <span>{result.intent.metric}</span>
                </div>
                <div className="meta-row">
                  <span className="meta-key">Rows</span>
                  <span>{result.row_count}</span>
                </div>
                <div className="meta-row">
                  <span className="meta-key">Order</span>
                  <span>{result.intent.order_by}</span>
                </div>
              </div>
            </section>
          </div>
        </div>
      ) : (
        <section className="panel empty">
          Submit a question to inspect the API response, generated SQL, table rows, and derived plot spec.
        </section>
      )}
    </div>
  );
}
