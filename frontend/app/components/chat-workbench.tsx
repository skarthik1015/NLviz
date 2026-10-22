"use client";

import { motion } from "framer-motion";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Table2, Layers, MessageSquarePlus } from "lucide-react";

import { ApiError, getSchema, streamChatQuestion, streamClarificationAnswer } from "../lib/api";
import { useConnection } from "../lib/connection-context";
import { AgentStep, ChatResponse, SchemaResponse } from "../lib/types";
import { PlotPreview } from "./plot-preview";
import { ResultsTable } from "./results-table";
import { TracePanel } from "./trace-panel";
import { InterpretationPanel } from "./interpretation-panel";

/** One question and its answer (or the clarification it is waiting on). */
type Turn = { question: string; response: ChatResponse; steps: AgentStep[] };

function newConversationId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function turnTitle(response: ChatResponse): string {
  if (response.interpretation?.path === "generated_sql") return "Generated SQL answer";
  const intent = response.intent;
  return (
    intent.metric.replaceAll("_", " ") +
    (intent.dimensions.length > 0 ? ` by ${intent.dimensions.join(", ").replaceAll("_", " ")}` : "") +
    (intent.time_dimension ? " over time" : "")
  );
}

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
  // The backend checks that each suggestion compiles and does not double count across joins.
  if (schema.suggested_questions && schema.suggested_questions.length > 0) {
    return schema.suggested_questions.slice(0, 6);
  }

  const metric = schema.metrics[0]?.display_name ?? schema.metrics[0]?.name ?? "value";
  const dimension = schema.dimensions[0]?.display_name ?? null;
  const dimension2 = schema.dimensions[1]?.display_name ?? dimension;
  const timeDimension = schema.time_dimensions[0]?.display_name ?? null;

  const examples: string[] = [];
  if (dimension) examples.push(`Show ${metric} by ${dimension}`);
  if (dimension2 && dimension2 !== dimension) examples.push(`Top 10 ${dimension2} by ${metric}`);
  if (timeDimension) examples.push(`Show ${metric} over time by ${timeDimension}`);
  examples.push(`What is the ${metric}?`);

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
  // The conversation: follow-up questions in the same conversation build on the previous turn.
  const [conversationId, setConversationId] = useState<string>(newConversationId);
  const [turns, setTurns] = useState<Turn[]>([]);
  // "Use this definition for every question on this connection" on a definition question.
  const [rememberDefinition, setRememberDefinition] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [liveSteps, setLiveSteps] = useState<AgentStep[]>([]);
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
  const current = turns.length ? turns[selected ?? turns.length - 1] : null;
  const result = current?.response ?? null;
  const isLatest = selected === null || selected === turns.length - 1;
  // Each connection switch starts a new generation and aborts the request in flight: a slow answer
  // for connection A must not land in B's conversation (review B16).
  const generationRef = useRef(0);
  const inFlightRef = useRef<AbortController | null>(null);

  /** A request bound to the current generation: `current()` is false once the user switched away. */
  function beginRequest() {
    inFlightRef.current?.abort();
    const controller = new AbortController();
    inFlightRef.current = controller;
    const generation = generationRef.current;
    return { signal: controller.signal, current: () => generation === generationRef.current && !controller.signal.aborted };
  }

  function startNewConversation() {
    setConversationId(newConversationId());
    setTurns([]);
    setSelected(null);
    setLiveSteps([]);
    setError(null);
  }

  // A new connection starts a new conversation.
  useEffect(() => {
    generationRef.current += 1;
    inFlightRef.current?.abort();
    inFlightRef.current = null;
    setIsSubmitting(false);
    startNewConversation();
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
      .catch((schemaError: unknown) => {
        if (cancelled) return;
        setSchema(null);
        // A published schema whose file was lost cannot answer anything: say how to fix it.
        if (schemaError instanceof ApiError && schemaError.code === "SCHEMA_MISSING") {
          setError("This connection's published schema is missing. Generate and publish its schema again.");
        }
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
    setLiveSteps([]);
    setSelected(null);

    const steps: AgentStep[] = [];
    const request = beginRequest();
    try {
      const response = await streamChatQuestion(
        trimmed,
        { conversationId, debug: showDebug, connectionId: activeConnectionId ?? undefined, signal: request.signal },
        (step) => {
          if (!request.current()) return;
          steps.push(step);
          setLiveSteps([...steps]);
        },
      );
      if (!request.current()) return;
      setTurns((previous) => [...previous, { question: trimmed, response, steps }]);
      setQuestion("");
    } catch (submissionError) {
      if (!request.current()) return;
      setError(
        submissionError instanceof Error
          ? submissionError.message
          : "The request failed before the backend returned a response.",
      );
    } finally {
      if (request.current()) {
        setIsSubmitting(false);
        setLiveSteps([]);
      }
    }
  }

  /** Answer the clarification the latest turn is waiting on; the turn continues from there. */
  async function answerClarification(answer: string, remember = false) {
    const last = turns[turns.length - 1];
    if (!last?.response.conversation_id) return;
    setError(null);
    setIsSubmitting(true);
    const steps: AgentStep[] = [...last.steps];
    setLiveSteps([...steps]);
    const request = beginRequest();
    try {
      const response = await streamClarificationAnswer(
        last.response.conversation_id,
        answer,
        { debug: showDebug, connectionId: activeConnectionId ?? undefined, signal: request.signal, remember },
        (step) => {
          if (!request.current()) return;
          steps.push(step);
          setLiveSteps([...steps]);
        },
      );
      if (!request.current()) return;
      setTurns((previous) => [...previous.slice(0, -1), { question: last.question, response, steps }]);
    } catch (submissionError) {
      if (!request.current()) return;
      setError(submissionError instanceof Error ? submissionError.message : "The request failed.");
    } finally {
      if (request.current()) {
        setIsSubmitting(false);
        setLiveSteps([]);
      }
    }
  }

  const traceSteps = isSubmitting ? liveSteps : current?.steps ?? [];

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
              placeholder={
                turns.length
                  ? "Ask a follow-up, e.g. “now by month” or “only credit card payments”"
                  : `For example: ${examples[0]}`
              }
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
              {isSubmitting
                ? "Running the agent…"
                : turns.length
                  ? `Conversation: ${turns.length} question${turns.length > 1 ? "s" : ""}. Follow-ups build on the last answer.`
                  : "Ask a question about this connection."}
            </div>
            {turns.length > 0 && (
              <button
                type="button"
                className="data-browser-btn"
                onClick={startNewConversation}
                disabled={isSubmitting}
                title="Start over: the next question will not build on earlier ones"
              >
                <MessageSquarePlus className="h-3.5 w-3.5" />
                New conversation
              </button>
            )}
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
              {isSubmitting ? "Querying…" : turns.length ? "Ask" : "Run Query"}
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

      {/* ── Conversation thread ────────────────────────────── */}
      {turns.length > 1 && (
        <section className="panel section">
          <h2 className="section-title">Conversation</h2>
          <ol className="flex flex-col gap-1.5">
            {turns.map((turn, index) => {
              const active = index === (selected ?? turns.length - 1);
              return (
                <li key={`${index}-${turn.response.query_id}`}>
                  <button
                    type="button"
                    onClick={() => setSelected(index)}
                    className={`w-full rounded-md border px-3 py-2 text-left text-sm transition-colors ${
                      active ? "border-primary/50 bg-background/80" : "border-border/40 hover:bg-background/60"
                    }`}
                  >
                    <span className="font-medium">{turn.question}</span>
                    <span style={{ color: "var(--muted)" }}>
                      {" → "}
                      {turn.response.status === "clarification"
                        ? "waiting for your answer"
                        : turn.response.status === "unsupported"
                          ? "not in this data"
                          : `${turnTitle(turn.response)} · ${turn.response.row_count} rows`}
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        </section>
      )}

      {/* ── Live progress ──────────────────────────────────── */}
      {isSubmitting && (
        <section className="panel section">
          <h2 className="section-title">Working on it</h2>
          <TracePanel steps={liveSteps} live />
        </section>
      )}

      {/* ── Clarification ──────────────────────────────────── */}
      {!isSubmitting && result?.status === "clarification" && result.clarification && (
        <section className="panel section">
          <h2 className="section-title">{result.clarification.prompt}</h2>
          {result.clarification.kind === "choice" ? (
            <div className="flex flex-wrap gap-2" data-testid="clarification-choices">
              {result.clarification.choices.map((choice) => (
                <button
                  key={choice.id}
                  type="button"
                  className="pill"
                  disabled={!isLatest}
                  onClick={() => answerClarification(choice.id, rememberDefinition && !!result.clarification?.rememberable)}
                >
                  {choice.label}
                </button>
              ))}
              {result.clarification.rememberable && (
                <label className="flex w-full items-center gap-2 text-sm" style={{ color: "var(--muted)" }}>
                  <input
                    type="checkbox"
                    checked={rememberDefinition}
                    onChange={(event) => setRememberDefinition(event.target.checked)}
                  />
                  Use this definition for every question on this connection (otherwise only this conversation)
                </label>
              )}
            </div>
          ) : (
          <>
          <p className="mb-3 text-sm" style={{ color: "var(--muted)" }}>
            {result.clarification.reason === "assumed"
              ? `Your question could refer to several ${(result.clarification.dimension ?? "").replaceAll("_", " ")} values. Pick one, or keep all of them.`
              : `“${result.clarification.raw}” is not a ${(result.clarification.dimension ?? "").replaceAll("_", " ")} in the data. Did you mean one of these?`}
          </p>
          <div className="flex flex-wrap gap-2">
            {result.clarification.options.map((option) => (
              <button
                key={option}
                type="button"
                className="pill"
                disabled={!isLatest}
                onClick={() => answerClarification(option)}
              >
                {option}
              </button>
            ))}
            <button
              type="button"
              className="pill"
              disabled={!isLatest}
              onClick={() => answerClarification(result.clarification!.any_value)}
            >
              Any {(result.clarification.dimension ?? "").replaceAll("_", " ")}
            </button>
          </div>
          </>
          )}
          <div className="mt-4">
            <TracePanel steps={current?.steps ?? []} live={false} />
          </div>
        </section>
      )}

      {/* ── Results ────────────────────────────────────────── */}
      {/* ── The data cannot answer the question ───────────── */}
      {!isSubmitting && result?.status === "unsupported" && (
        <section className="panel section" data-testid="unsupported">
          <h2 className="section-title">Not something this data can answer</h2>
          <p style={{ fontSize: "0.9rem", color: "var(--muted)", margin: 0, lineHeight: 1.5 }}>
            {result.explanation ?? result.intent.unsupported ?? "This question needs data or a definition that isn't available. Try a measure or breakdown from View Schema."}
          </p>
        </section>
      )}

      {result?.status === "clarification" || result?.status === "unsupported" ? null : result ? (
        <div className="results-grid">
          <section className="panel section chart-panel">
            <h2 className="section-title">{turnTitle(result)}</h2>

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
              {result.interpretation && <InterpretationPanel interpretation={result.interpretation} />}
              <section className="panel section">
                <h2 className="section-title">How it was answered</h2>
                <TracePanel
                  steps={traceSteps.length ? traceSteps : result.trace.map((line) => ({ node: "", trace: [line] }))}
                  live={false}
                  debugTrace={showDebug ? result.debug_trace : null}
                />
              </section>

              <section className="panel section">
                <h2 className="section-title">{result.interpretation?.path === "generated_sql" ? "Generated SQL" : result.interpretation?.path === "sql" ? "Checked SQL" : "Compiled SQL"}</h2>
                <pre className="code">{result.sql ?? ""}</pre>
              </section>

              <section className="panel section">
                <h2 className="section-title">Query Metadata</h2>
                <div className="meta-grid">
                  <div className="meta-row">
                    <span className="meta-key">Query ID</span>
                    <span className="meta-val">{result.query_id}</span>
                  </div>
                  <div className="meta-row">
                    <span className="meta-key">Answered by</span>
                    <span>
                      {result.interpretation?.path === "sql"
                        ? "SQL written by the model, checked against the data model"
                        : result.interpretation?.path === "generated_sql"
                          ? "Generated SQL (not checked)"
                          : "Semantic compiler"}
                    </span>
                  </div>
                  {result.interpretation?.path !== "sql" && (
                    <>
                      {/* The compiler path's own fields; the SQL path's are in the interpretation. */}
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
                    </>
                  )}
                  <div className="meta-row">
                    <span className="meta-key">Rows</span>
                    <span>{result.row_count}</span>
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
