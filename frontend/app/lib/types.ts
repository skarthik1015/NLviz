export type FilterCondition = {
  dimension: string;
  operator:
    | "eq"
    | "ne"
    | "in"
    | "not_in"
    | "gt"
    | "gte"
    | "lt"
    | "lte"
    | "contains"
    | "between"
    | "is_null"
    | "is_not_null";
  /** None for is_null / is_not_null. Lists hold strings and numbers (as the backend model). */
  value: string | number | boolean | Array<string | number> | null;
};

/** A threshold on a metric per group, compiled to HAVING ("albums with more than 5 tracks"). */
export type MetricFilter = {
  metric: string;
  operator: "eq" | "ne" | "gt" | "gte" | "lt" | "lte";
  value: number;
};

export type SemanticIntent = {
  metric: string;
  dimensions: string[];
  filters: FilterCondition[];
  time_dimension: string | null;
  time_granularity: "day" | "week" | "month" | "quarter" | "year" | null;
  start_date: string | null;
  end_date: string | null;
  order_by: "metric_desc" | "metric_asc" | "time_asc" | "time_desc";
  limit: number;
  metric_filters?: MetricFilter[];
  /** Set when the data cannot answer the question: what is missing. */
  unsupported?: string | null;
};

/** Plotly figure JSON as returned by the backend chart_selector node. */
export type PlotlyFigure = {
  data: unknown[];
  layout: Record<string, unknown>;
  frames?: unknown[];
};

export type ChatResponse = {
  query_id: string;
  question: string;
  intent: SemanticIntent;
  intent_source: "heuristic" | "llm" | "llm_fallback" | "explicit";
  /** "clarification": the turn is paused on `clarification`; answer it to continue. */
  status: "answered" | "clarification" | "unsupported";
  conversation_id: string | null;
  clarification: Clarification | null;
  sql: string | null;
  rows: Array<Record<string, unknown>>;
  row_count: number;
  trace: string[];
  validation_status: string;
  chart_spec: PlotlyFigure | null;
  explanation: string | null;
  debug_trace: string[] | null;
  // Only with debug: the run (model, phrase rules, semantic layer version), each step that changed
  // the intent, and tokens per LLM call. Not shown in the UI; read by the eval harnesses.
  audit?: Record<string, unknown> | null;
  evidence: EvidenceItem[];
  // How the answer was understood (plan step 1a); null for a refusal. Mirrors
  // backend/app/models/responses.py Interpretation.
  interpretation?: Interpretation | null;
};

export type InterpretedField = { name: string; label: string };

export type InterpretedMetric = { name: string; label: string; aggregation: string; definition: string; description: string };

export type Interpretation = {
  // "semantic": the semantic layer and compiler; "sql": SQL the model wrote, held to the layer's
  // definitions and read back from that SQL; "generated_sql": the unchecked fallback (metric is null)
  path: "semantic" | "sql" | "generated_sql";
  metric: InterpretedMetric | null;
  measures?: InterpretedMetric[]; // "sql": every metric of the layer the query computes
  sql?: string | null;
  filters: { dimension: string; label: string; operator: string; value: unknown }[];
  thresholds: string[];
  grouping: InterpretedField[];
  time_grain: InterpretedField | null;
  period: { column: string; label: string; start: string | null; end: string | null } | null; // ISO dates, end inclusive
  order_by: string;
  limit: number;
  policies: { name: string; statement: string; source: "inferred" | "confirmed" | "conversation" | "question" | "unknown" }[];
  notes: string[];
};

export type Clarification = {
  /** "value": which value of `dimension` (answer with an option or `any_value`). "choice": an
   * ambiguity the data reveals, e.g. amounts in several currencies (answer with a choice id). */
  kind: "value" | "choice";
  /** "assumed": the agent picked `raw`, a real value the question does not name, among several. */
  reason: "no_match" | "assumed" | "mixed_units" | "fan_out" | "zero_placeholder" | "definition";
  /** A definition question whose answer may be kept for every question on this connection. */
  rememberable?: boolean;
  prompt: string;
  dimension: string | null;
  raw: string;
  options: string[];
  /** Answer that drops the filter instead of choosing a value. */
  any_value: string;
  choices: Array<{ id: string; label: string }>;
};

/** How a value in the question was matched to the data. */
export type EvidenceItem =
  | { kind: "hint"; dimension: string; value: string }
  | {
      kind: "match";
      dimension: string;
      text: string;
      value: string | null;
      confidence: "exact" | "agree" | "weak" | "none" | "meaning";
      alternatives: string[];
    }
  | { kind: "clarification"; dimension: string; text: string; value: string | null }
  /** Something the answer must say, e.g. the currency its amounts are in. */
  | { kind: "note"; dimension: string; text: string };

/** One agent step, streamed while a question runs. */
export type AgentStep = {
  node: string;
  trace: string[];
  ms?: number;
  evidence?: EvidenceItem[];
  debug_trace?: string[];
};

// ── Connection management types ─────────────────────────────────────

// "duckdb" and "athena" remain only on connections created before Jammi (v1): listed, not served.
export type ConnectorType = "duckdb" | "postgres" | "mysql" | "upload" | "athena";
/** What can be tested and created as a database connection today. */
export type DatabaseConnectorType = "postgres" | "mysql";

export type User = {
  user_id: string;
  email: string | null;
};

export type ConnectionProfile = {
  connection_id: string;
  display_name: string;
  connector_type: ConnectorType;
  created_at: string;
  owner_id: string | null;
  status: "active" | "archived";
  query_ready: boolean;
  /** "snapshot": answers come from a copy made at copied_at; notes say why. */
  mode?: "live" | "snapshot";
  copied_at?: string | null;
  notes?: string[];
};

export type ConnectionTestRequest = {
  connector_type: DatabaseConnectorType;
  params: Record<string, unknown>;
};

export type ConnectionTestResponse = {
  success: boolean;
  tables: Array<{ name: string; row_count: number; column_count: number }>;
  error: string | null;
};

export type ConnectionCreateRequest = {
  connector_type: DatabaseConnectorType;
  params: Record<string, unknown>;
  display_name: string;
};

export type AttachFileResponse = {
  connection_id: string;
  table: string;
  renamed_columns: Record<string, string>;
  schema_regeneration_required: boolean;
};

export type S3ImportRequest = {
  s3_url: string;
  display_name: string;
  region?: string;
  access_key_id?: string | null;
  secret_access_key?: string | null;
  session_token?: string | null;
};

export type ConnectionCreateResponse = {
  connection_id: string;
  status: string;
  // Tables Jammi could not register (mixed-case names), and columns renamed to lowercase identifiers.
  skipped_tables?: string[];
  renamed_columns?: Record<string, string>;
  /** "snapshot": copied when connected (MySQL, or Postgres types the engine cannot read live). */
  mode?: "live" | "snapshot";
  notes?: string[];
};

export type ValidationSummary = {
  total_metrics: number;
  valid_metrics: number;
  broken_metrics: string[];
  total_dimensions: number;
  valid_dimensions: number;
  broken_dimensions: string[];
  total_time_dimensions?: number;
  valid_time_dimensions?: number;
  broken_time_dimensions?: string[];
  total_filter_columns?: number;
  broken_filter_columns?: string[];
  confidence_score: number;
  /** The model that answers questions, after broken items were removed (the counts above are the proposal's). */
  published_metrics?: number | null;
  published_dimensions?: number | null;
  published_time_dimensions?: number | null;
  published_filter_columns?: number | null;
};

export type JobStatusResponse = {
  job_id: string;
  connection_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  error: string | null;
  schema_version_id: string | null;
  validation_summary: ValidationSummary | null;
};

export type GenerateResponse = {
  job_id: string;
  status: string;
};

export type SchemaResponse = {
  connector_type: string;
  tables: Record<string, Array<Record<string, unknown>>>;
  row_counts: Record<string, number>;
  join_paths: Array<Record<string, string>>;
  metrics: Array<{ name: string; display_name: string }>;
  dimensions: Array<{ name: string; display_name: string }>;
  time_dimensions: Array<{ name: string; display_name: string }>;
  /** Example questions the data model answers as asked (built by the backend). */
  suggested_questions?: string[];
  /** Value matching: "building" until the index is ready (answers come without it meanwhile). */
  grounding?: "ready" | "building" | "none" | "off";
};
