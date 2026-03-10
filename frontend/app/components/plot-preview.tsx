"use client";

import dynamic from "next/dynamic";
import type { PlotlyFigure } from "../lib/types";

// Dynamic import prevents Plotly from running server-side (it needs the DOM)
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Plot = dynamic<any>(
  // @ts-ignore — resolves after: npm install (in frontend/)
  () => import("react-plotly.js"),
  { ssr: false },
);

type PlotPreviewProps = {
  spec: PlotlyFigure | null;
  queryId: string | null;
};

export function PlotPreview({ spec, queryId }: PlotPreviewProps) {
  if (!spec) {
    return (
      <div className="chart-shell">
        <div className="chart-canvas chart-canvas--empty" style={{ display: "grid", placeItems: "center" }}>
          <div style={{ color: "var(--muted)", fontSize: "0.9rem" }}>No chart data available.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="chart-shell">
      <div className="chart-canvas">
        <Plot
          data={spec.data}
          layout={{
            ...spec.layout,
            autosize: true,
            dragmode: "pan",
            uirevision: queryId ?? "chart",
            paper_bgcolor: "rgba(0,0,0,0)",
            plot_bgcolor: "rgba(0,0,0,0)",
            font: { family: "inherit", size: 12 },
          }}
          config={{
            responsive: true,
            displayModeBar: true,
            scrollZoom: true,
            displaylogo: false,
            modeBarButtonsToRemove: ["lasso2d", "select2d"],
          }}
          style={{ width: "100%", height: "100%" }}
          useResizeHandler={true}
        />
      </div>
    </div>
  );
}
