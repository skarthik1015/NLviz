import type { Interpretation } from "../lib/types";

const OPERATORS: Record<string, string> = {
  eq: "=", ne: "≠", gt: ">", gte: "≥", lt: "<", lte: "≤",
  in: "in", not_in: "not in", contains: "contains", between: "between",
  is_null: "is empty", is_not_null: "is not empty",
  neq: "≠", like: "like", ilike: "like", sql: "",
};

function filterValue(value: unknown): string {
  if (value == null) return "";
  if (Array.isArray(value)) return value.map(filterValue).join(", ");
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/** The server's interpretation, kept with its answer; the browser does not infer anything from SQL
 * (for the "sql" path the server read it from the query that ran). */
export function InterpretationPanel({ interpretation }: { interpretation: Interpretation }) {
  const { metric, period } = interpretation;
  const measures = interpretation.measures ?? [];
  const details = [
    ["Computed", measures.length > 1
      ? measures.map((m) => `${m.label} (${m.aggregation})`).join("; ")
      : metric ? `${metric.label} (${metric.aggregation})` : "See generated SQL"],
    ...(metric ? [["Definition", metric.definition], ...(metric.description ? [["Description", metric.description]] : [])] : []),
    ["Filters", interpretation.filters.length
      ? interpretation.filters.map((filter) => `${filter.label} ${OPERATORS[filter.operator] ?? filter.operator}${["is_null", "is_not_null"].includes(filter.operator) ? "" : ` ${filterValue(filter.value)}`}`).join("; ")
      : "None"],
    ...(interpretation.thresholds.length ? [["Thresholds", interpretation.thresholds.join("; ")]] : []),
    ["Grouping", interpretation.grouping.map((field) => field.label).join(", ") || "None"],
    ...(interpretation.time_grain ? [["Time grain", interpretation.time_grain.label]] : []),
    ["Period", period
      ? `${period.label} (${period.column}), ${period.start && period.end ? `${period.start} to ${period.end}` : period.start ? `from ${period.start}` : period.end ? `through ${period.end}` : "all dates"}`
      : "All dates"],
    ["Order", interpretation.order_by || "As returned"],
    ["Limit", interpretation.limit ? String(interpretation.limit) : "None"],
  ];

  return (
    <section className="panel section" aria-label="Interpretation">
      <h2 className="section-title">Interpretation</h2>
      {interpretation.path === "generated_sql" && (
        <p className="mb-3 text-sm font-medium">Generated SQL, not checked against definitions</p>
      )}
      {interpretation.path === "sql" && (
        <p className="mb-3 text-sm font-medium">Read from the SQL below, checked against the data model&apos;s definitions</p>
      )}
      <dl className="meta-grid text-sm">
        {details.map(([label, value]) => (
          <div key={label} className="meta-row">
            <dt className="meta-key">{label}</dt>
            <dd className="min-w-0 break-words">{value}</dd>
          </div>
        ))}
      </dl>
      {interpretation.policies.length > 0 && (
        <div className="mt-3 text-sm">
          <h3 className="font-medium">Policies</h3>
          <ul className="mt-1 space-y-1">
            {interpretation.policies.map((policy, index) => (
              <li key={`${policy.name}-${index}`}>
                {policy.statement} <span className="pill text-xs">{policy.source}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {interpretation.notes.length > 0 && (
        <div className="mt-3 text-sm">
          <h3 className="font-medium">Notes</h3>
          <ul className="mt-1 space-y-1">
            {interpretation.notes.map((note, index) => <li key={index}>{note}</li>)}
          </ul>
        </div>
      )}
    </section>
  );
}
