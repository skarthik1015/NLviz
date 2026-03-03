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
  time_dimension: "order_date" | "delivery_date" | null;
  time_granularity: "day" | "week" | "month" | "quarter" | "year" | null;
  start_date: string | null;
  end_date: string | null;
  order_by: "metric_desc" | "metric_asc" | "time_asc" | "time_desc";
  limit: number;
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
};

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
