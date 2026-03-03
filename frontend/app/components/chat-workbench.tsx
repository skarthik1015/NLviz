"use client";

import { useState, type FormEvent } from "react";

import { sendChatQuestion } from "../lib/api";
import { buildPlotSpec } from "../lib/plot-spec";
import { ChatResponse, PlotSpec } from "../lib/types";
import { PlotPreview } from "./plot-preview";
import { ResultsTable } from "./results-table";

const EXAMPLES = [
  "Show total revenue by customer state",
  "Show order count by product category",
  "Show total revenue over time",
  "Top 10 categories by revenue",
  "Average delivery time by seller state",
  "Revenue in 2018 by product category",
];

export function ChatWorkbench() {
  const [question, setQuestion] = useState(EXAMPLES[0]);
  const [result, setResult] = useState<ChatResponse | null>(null);
  const [plotSpec, setPlotSpec] = useState<PlotSpec | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const trimmed = question.trim();
    if (!trimmed) {
      setError("Enter a question before submitting.");
      return;
    }

    setError(null);
    setIsSubmitting(true);

    try {
      const response = await sendChatQuestion(trimmed);
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
        <form onSubmit={handleSubmit} className="composer-grid">
          <label className="label">
            Ask a business question
            <textarea
              className="textarea"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="For example: Top 10 sellers by revenue"
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
        <div className="results-grid">
          <section className="panel section chart-panel">
            <h2 className="section-title">
              {result.intent.metric.replaceAll("_", " ")}
              {result.intent.dimensions.length > 0
                ? ` by ${result.intent.dimensions.join(", ").replaceAll("_", " ")}`
                : ""}
              {result.intent.time_dimension ? " over time" : ""}
            </h2>
            <div className="chart-row">
              <div className="chart-area">
                {plotSpec ? <PlotPreview spec={plotSpec} /> : <div className="empty">No chart available.</div>}
              </div>
              <div className="stats-sidebar">
                <div className="stat-card">
                  <div className="stat-label">Rows</div>
                  <div className="stat-value">{result.row_count}</div>
                </div>
                <div className="stat-card">
                  <div className="stat-label">Metric</div>
                  <div className="stat-value-sm">{result.intent.metric.replaceAll("_", " ")}</div>
                </div>
                <div className="stat-card">
                  <div className="stat-label">Dimensions</div>
                  <div className="stat-value-sm">
                    {result.intent.dimensions.length > 0
                      ? result.intent.dimensions.join(", ").replaceAll("_", " ")
                      : "none"}
                  </div>
                </div>
                {result.intent.start_date && (
                  <div className="stat-card">
                    <div className="stat-label">Date filter</div>
                    <div className="stat-value-sm">
                      {result.intent.start_date} {"->"} {result.intent.end_date}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </section>

          <div className="lower-grid">
            <section className="panel section">
              <h2 className="section-title">Tabular Result</h2>
              <ResultsTable rows={result.rows} />
            </section>

            <div className="stack">
              <section className="panel section">
                <h2 className="section-title">Execution Trace</h2>
                <div className="trace">
                  {result.trace.map((item, index) => (
                    <div className="trace-item" key={`${index}-${item}`}>
                      {item}
                    </div>
                  ))}
                </div>
              </section>

              <section className="panel section">
                <h2 className="section-title">Compiled SQL</h2>
                <pre className="code">{result.sql}</pre>
              </section>

              <section className="panel section">
                <h2 className="section-title">Query Metadata</h2>
                <div className="meta-grid">
                  <div className="meta-row">
                    <span className="meta-key">Query ID</span>
                    <span className="meta-val">{result.query_id}</span>
                  </div>
                  <div className="meta-row">
                    <span className="meta-key">Order</span>
                    <span>{result.intent.order_by}</span>
                  </div>
                  <div className="meta-row">
                    <span className="meta-key">Intent source</span>
                    <span>{result.intent_source}</span>
                  </div>
                  <div className="meta-row">
                    <span className="meta-key">Limit</span>
                    <span>{result.intent.limit}</span>
                  </div>
                </div>
              </section>
            </div>
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
