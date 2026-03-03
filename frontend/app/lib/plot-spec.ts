import { ChatResponse, PlotSpec, PlotSeriesDatum } from "./types";

function toDisplayLabel(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Unspecified";
  }
  return String(value);
}

function toNumericMetric(value: unknown): number {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : 0;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function buildPlotSpec(response: ChatResponse): PlotSpec {
  const firstRow = response.rows[0] ?? {};
  const candidateKeys = Object.keys(firstRow).filter((key) => key !== "metric_value");
  const xKey = candidateKeys[0] ?? null;

  const chartType: PlotSpec["chart_type"] = response.intent.time_dimension
    ? "line"
    : xKey
      ? "bar"
      : "stat";

  const series: PlotSeriesDatum[] = xKey
    ? response.rows.map((row) => ({
        label: toDisplayLabel(row[xKey]),
        value: toNumericMetric(row.metric_value),
      }))
    : [
        {
          label: response.intent.metric,
          value: toNumericMetric(firstRow.metric_value),
        },
      ];

  return {
    chart_type: chartType,
    title: `${response.intent.metric.replaceAll("_", " ")} overview`,
    x_key: xKey,
    y_key: "metric_value",
    series,
  };
}
