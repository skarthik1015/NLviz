"use client";

import type { AgentStep, EvidenceItem } from "../lib/types";

const STEP_LABELS: Record<string, string> = {
  map_intent: "Understanding the question",
  check_intent: "Checking against the data model",
  clarify: "Your answer",
  sql_builder: "Compiling SQL",
  executor: "Running on Jammi",
  validator: "Checking the result",
  chart_selector: "Choosing a chart",
  explainer: "Summarising",
  sql_answer: "Writing and checking the query",
  sql_clarify: "Your answer",
};

const label = (name: string) => name.replaceAll("_", " ");

function describeEvidence(item: EvidenceItem): string | null {
  switch (item.kind) {
    case "hint":
      return `Found ${label(item.dimension)} “${item.value}” in the question`;
    case "match":
      if (item.value === null) {
        return item.alternatives.length
          ? `“${item.text}” is not a known ${label(item.dimension)}; closest: ${item.alternatives.join(", ")}`
          : `“${item.text}” is not a known ${label(item.dimension)}`;
      }
      if (item.value === item.text) return `${label(item.dimension)} = ${item.value} (exact)`;
      if (item.confidence === "meaning") return `“${item.text}” → ${label(item.dimension)}: ${item.value} (matched by meaning)`;
      return `“${item.text}” → ${label(item.dimension)} = ${item.value} (${item.confidence === "exact" ? "synonym" : "matched by search"})`;
    case "clarification":
      return item.value === null
        ? `You chose any ${label(item.dimension)} for “${item.text}”`
        : `You chose ${item.value} for “${item.text}”`;
    case "note":
      return item.text;
  }
}

/** The agent's steps as they ran (with durations) and how values in the question were matched. */
export function TracePanel({ steps, live, debugTrace }: { steps: AgentStep[]; live: boolean; debugTrace?: string[] | null }) {
  const evidence = steps.flatMap((step) => step.evidence ?? []);
  const lines = [...new Set(evidence.map(describeEvidence).filter((line): line is string => line !== null))];
  const total = steps.reduce((sum, step) => sum + (step.ms ?? 0), 0);

  return (
    <div className="flex flex-col gap-3">
      <ol className="trace" aria-live={live ? "polite" : undefined}>
        {steps.map((step, index) => (
          <li className="trace-item !py-2" key={`${index}-${step.node}`}>
            <div className="flex items-baseline justify-between gap-3">
              <span className="font-medium">{STEP_LABELS[step.node] ?? label(step.node)}</span>
              {step.ms !== undefined && (
                <span className="text-xs tabular-nums" style={{ color: "var(--muted)" }}>
                  {step.ms} ms
                </span>
              )}
            </div>
            {step.trace.map((line, lineIndex) => (
              <div key={lineIndex} className="text-xs" style={{ color: "var(--muted)" }}>
                {line}
              </div>
            ))}
          </li>
        ))}
        {live && <li className="trace-item animate-pulse" style={{ color: "var(--muted)" }}>Working…</li>}
      </ol>
      {!live && steps.length > 0 && (
        <div className="text-xs" style={{ color: "var(--muted)" }}>
          {steps.length} steps · {(total / 1000).toFixed(1)} s
        </div>
      )}
      {lines.length > 0 && (
        <div>
          <div className="stat-label">Values matched to the data</div>
          <ul className="mt-1 flex flex-col gap-1 text-sm">
            {lines.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      )}
      {debugTrace && debugTrace.length > 0 && (
        <details>
          <summary className="cursor-pointer text-xs" style={{ color: "var(--muted)" }}>
            Debug trace
          </summary>
          <pre className="code mt-2 whitespace-pre-wrap text-xs">{debugTrace.join("\n")}</pre>
        </details>
      )}
    </div>
  );
}
