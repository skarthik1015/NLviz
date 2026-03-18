"use client";

import { motion } from "framer-motion";
import { useEffect, useState, type FormEvent } from "react";
import { Table2, Layers } from "lucide-react";

import { getSchema, sendChatQuestion } from "../lib/api";
import { useConnection } from "../lib/connection-context";
import { ChatResponse, SchemaResponse } from "../lib/types";
import { PlotPreview } from "./plot-preview";
import { ResultsTable } from "./results-table";

function TableBrowserPanel({ schema }: { schema: SchemaResponse }) {
  const tableNames = Object.keys(schema.tables);
  const [activeTable, setActiveTable] = useState<string>(tableNames[0] ?? "");
  const columns = activeTable ? (schema.tables[activeTable] ?? []) : [];

  if (tableNames.length === 0) return null;

  return (
    <div className="data-browser-panel">
      {/* Table tabs */}
      <div className="db-tab-strip">
        {tableNames.map((tbl) => (
          <button
            key={tbl}
            type="button"
            onClick={() => setActiveTable(tbl)}
            className={`db-tab${activeTable === tbl ? " active" : ""}`}
          >
            {tbl}
            {schema.row_counts[tbl] != null && (
              <span className="db-tab-badge">{schema.row_counts[tbl].toLocaleString()}</span>
            )}
          </button>
        ))}
      </div>

      {/* Column table */}
      {columns.length > 0 && (
        <div className="db-col-table-wrap">
          <table className="db-col-table">
            <thead>
              <tr>
                <th>Column</th>
                <th>Type</th>
                <th>Sample values</th>
              </tr>
            </thead>
            <tbody>
              {columns.map((col, i) => {
                const name = (col.name ?? col.column_name ?? "?") as string;
                const type = (col.type ?? col.data_type ?? "") as string;
                const samples = (col.sample_values ?? []) as unknown[];
                return (
                  <tr key={`${name}-${i}`}>
                    <td className="db-col-name">{name}</td>
                    <td className="db-col-type">{type}</td>
                    <td className="db-col-samples">
                      {samples
                        .slice(0, 4)
                        .map((s) => String(s ?? ""))
                        .filter(Boolean)
                        .join(", ") || "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SchemaViewerPanel({ schema }: { schema: SchemaResponse }) {
  const sections: Array<{ label: string; items: Array<{ name: string; display_name: string }> }> = [
    { label: "Metrics", items: schema.metrics },
    { label: "Dimensions", items: schema.dimensions },
    { label: "Time Dimensions", items: schema.time_dimensions },
  ];

  return (
    <div className="data-browser-panel schema-viewer-panel">
      {sections.map(({ label, items }) =>
        items.length === 0 ? null : (
          <div key={label} className="schema-section">
            <div className="schema-section-label">{label}</div>
            <div className="schema-pills">
              {items.map((item) => (
                <span key={item.name} className="schema-pill">
                  {item.display_name}
                </span>
              ))}
            </div>
          </div>
        )
      )}
    </div>
  );
}

function buildExamples(schema: SchemaResponse | null): string[] {
  if (!schema || schema.metrics.length === 0) {
    return [];
  }

  const metric = schema.metrics[0]?.display_name ?? schema.metrics[0]?.name ?? "value";
  const metric2 = schema.metrics[1]?.display_name ?? metric;
  const dimension = schema.dimensions[0]?.display_name ?? null;
  const dimension2 = schema.dimensions[1]?.display_name ?? dimension;
  const timeDimension = schema.time_dimensions[0]?.display_name ?? null;

  const examples: string[] = [];
  if (dimension) examples.push(`Show ${metric} by ${dimension}`);
  if (dimension2 && dimension2 !== dimension) examples.push(`Top 10 ${dimension2} by ${metric}`);
  if (timeDimension) examples.push(`Show ${metric} over time by ${timeDimension}`);
  examples.push(`What is the ${metric}?`);
  if (timeDimension && dimension) examples.push(`${metric2} in 2018 by ${dimension}`);

  return examples.slice(0, 6);
}

const UNSAFE_PATTERNS = [
  /ignore\s+previous\s+instructions/i,
  /system\s+prompt/i,
  /developer\s+instructions/i,
  /jailbreak/i,
  /bypass\s+safety/i,
];

function looksUnsafeQuery(input: string): boolean {
  return UNSAFE_PATTERNS.some((pattern) => pattern.test(input));
}

export function ChatWorkbench() {
  const { activeConnectionId, connections } = useConnection();
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<ChatResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [showDebug, setShowDebug] = useState(false);
  const [unsafeWarning, setUnsafeWarning] = useState<string | null>(null);
  const [allowUnsafeSubmit, setAllowUnsafeSubmit] = useState(false);
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [showTableBrowser, setShowTableBrowser] = useState(false);
  const [showSchemaViewer, setShowSchemaViewer] = useState(false);
  const activeConnection = connections.find((conn) => conn.connection_id === activeConnectionId);
  const canQuery = activeConnection?.query_ready ?? activeConnectionId == null;
  const examples = buildExamples(schema);

  // Clear stale results when switching connections
  useEffect(() => {
    setResult(null);
    setError(null);
    setQuestion("");
    setShowTableBrowser(false);
    setShowSchemaViewer(false);
  }, [activeConnectionId]);

  useEffect(() => {
    let cancelled = false;
    if (!canQuery) {
      setSchema(null);
      return;
    }
    getSchema(activeConnectionId ?? undefined)
      .then((nextSchema) => {
        if (!cancelled) {
          setSchema(nextSchema);
          setQuestion((current: string) => {
            const first = buildExamples(nextSchema)[0];
            return current.trim() ? current : (first ?? "");
          });
        }
      })
      .catch(() => {
        if (!cancelled) setSchema(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeConnectionId, canQuery]);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!canQuery) {
      setError("Generate and publish a schema before querying this connection.");
      return;
    }
    const trimmed = question.trim();
    if (!trimmed) {
      setError("Enter a question before submitting.");
      return;
    }

    if (looksUnsafeQuery(trimmed) && !allowUnsafeSubmit) {
      setUnsafeWarning(
        "That looks like a system instruction, not a data question. Confirm if you still want to submit.",
      );
      setError("Invalid/ Unsafe Query");
      return;
    }

    setUnsafeWarning(null);
    setAllowUnsafeSubmit(false);
    setError(null);
    setIsSubmitting(true);

    try {
      const response = await sendChatQuestion(trimmed, showDebug, activeConnectionId ?? undefined);
      setResult(response);
    } catch (submissionError) {
      setResult(null);
      setError(
        submissionError instanceof Error
          ? submissionError.message
          : "The request failed before the backend returned a response.",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  const traceItems = showDebug && result?.debug_trace ? result.debug_trace : result?.trace ?? [];

  return (
    <div className="workspace">
      {/* ── Composer panel ─────────────────────────────────── */}
      <section className="panel composer">
        <form onSubmit={handleSubmit} className="composer-grid">
          <label className="label">
            Ask a business question
            <textarea
              className="textarea"
              value={question}
              onChange={(event) => {
                setQuestion(event.target.value);
                setUnsafeWarning(null);
                setAllowUnsafeSubmit(false);
              }}
              placeholder={`For example: ${examples[0]}`}
            />
          </label>

          {/* Prompt pills */}
          <div className="flex flex-wrap gap-2">
            {examples.map((example) => (
              <motion.button
                key={example}
                type="button"
                whileHover={{ scale: 1.03 }}
                whileTap={{ scale: 0.97 }}
                onClick={() => {
                  setQuestion(example);
                  setUnsafeWarning(null);
                  setAllowUnsafeSubmit(false);
                }}
                disabled={isSubmitting}
                className="rounded-full border border-border/40 bg-background/60 px-3.5 py-1.5 text-xs font-medium text-foreground/70 backdrop-blur-sm transition-colors hover:border-primary/30 hover:bg-background/80 hover:text-foreground disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {example}
              </motion.button>
            ))}
          </div>

          {/* Data browser controls */}
          {schema && (
            <div className="data-browser-controls">
              <button
                type="button"
                className={`data-browser-btn${showTableBrowser ? " active" : ""}`}
                onClick={() => setShowTableBrowser((v) => !v)}
              >
                <Table2 className="h-3.5 w-3.5" />
                Browse Tables
              </button>
              <button
                type="button"
                className={`data-browser-btn${showSchemaViewer ? " active" : ""}`}
                onClick={() => setShowSchemaViewer((v) => !v)}
              >
                <Layers className="h-3.5 w-3.5" />
                View Schema
              </button>
            </div>
          )}

          {/* Table browser panel */}
          {showTableBrowser && schema && <TableBrowserPanel schema={schema} />}

          {/* Schema viewer panel */}
          {showSchemaViewer && schema && <SchemaViewerPanel schema={schema} />}

          {/* Toolbar */}
          <div className="toolbar">
            <div className="status">
              {isSubmitting ? "Running semantic query pipeline…" : "Ready to query the backend API."}
            </div>
            <label className="flex items-center gap-1.5 text-sm" style={{ color: "var(--muted)" }}>
              <input type="checkbox" checked={showDebug} onChange={(e) => setShowDebug(e.target.checked)} />
              Debug trace
            </label>
            <motion.button
              className="button"
              type="submit"
              disabled={isSubmitting || !canQuery}
              whileHover={!isSubmitting && canQuery ? { translateY: -1 } : {}}
              whileTap={!isSubmitting && canQuery ? { scale: 0.97 } : {}}
            >
              {isSubmitting ? "Querying…" : "Run Query"}
            </motion.button>
            {!canQuery && (
              <span style={{ fontSize: "0.82rem", color: "var(--muted)" }}>
                This connection is not query-ready yet.
              </span>
            )}
          </div>

          {unsafeWarning ? (
            <div className="error">
              {unsafeWarning}{" "}
              <button
                type="button"
                className="pill"
                onClick={() => {
                  setAllowUnsafeSubmit(true);
                  setError(null);
                }}
                disabled={isSubmitting}
              >
                Confirm Intent
              </button>
            </div>
          ) : null}
          {error ? <div className="error">{error}</div> : null}
        </form>
      </section>

      {/* ── Results ────────────────────────────────────────── */}
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

            {result.explanation ? (
              <p style={{ fontSize: "0.9rem", color: "var(--muted)", margin: "0 0 12px 0", lineHeight: 1.5 }}>
                {result.explanation}
              </p>
            ) : null}

            <div className="chart-row">
              <div className="chart-area">
                <PlotPreview spec={result.chart_spec} queryId={result.query_id} />
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
                      {result.intent.start_date} {"→"} {result.intent.end_date}
                    </div>
                  </div>
                )}
                {result.validation_status && result.validation_status !== "ok" && (
                  <div className="stat-card">
                    <div className="stat-label">Validation</div>
                    <div className="stat-value-sm">{result.validation_status}</div>
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
                <h2 className="section-title">
                  {showDebug && result.debug_trace ? "Debug Trace" : "Execution Trace"}
                </h2>
                <div className="trace">
                  {traceItems.map((item, index) => (
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
          Submit a question to inspect the API response, generated SQL, table rows, and chart.
        </section>
      )}
    </div>
  );
}
