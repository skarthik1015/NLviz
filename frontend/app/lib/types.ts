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
    | "between";
  value: string | number | boolean | Array<string | number | boolean>;
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
  sql: string;
  rows: Array<Record<string, unknown>>;
  row_count: number;
  trace: string[];
  validation_status: string;
  chart_spec: PlotlyFigure | null;
  explanation: string | null;
  debug_trace: string[] | null;
};

// ── Connection management types ─────────────────────────────────────

export type ConnectorType = "duckdb" | "postgres" | "athena";

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
};

export type ConnectionTestRequest = {
  connector_type: ConnectorType;
  params: Record<string, unknown>;
};

export type ConnectionTestResponse = {
  success: boolean;
  tables: Array<{ name: string; row_count: number; column_count: number }>;
  error: string | null;
};

export type ConnectionCreateRequest = {
  connector_type: ConnectorType;
  params: Record<string, unknown>;
  display_name: string;
};

export type ConnectionCreateResponse = {
  connection_id: string;
  status: string;
};

export type ValidationSummary = {
  total_metrics: number;
  valid_metrics: number;
  broken_metrics: string[];
  total_dimensions: number;
  valid_dimensions: number;
  broken_dimensions: string[];
  confidence_score: number;
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

// Legacy types kept for plot-spec.ts fallback — no longer used in main UI
export type PlotSeriesDatum = {
  label: string;
  value: number;
};

export type PlotSpec = {
  chart_type: "line" | "bar" | "stat";
  title: string;
  x_key: string | null;
  y_key: "metric_value";
  series: PlotSeriesDatum[];
};
