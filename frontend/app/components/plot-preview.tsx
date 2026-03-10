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
};

export function PlotPreview({ spec }: PlotPreviewProps) {
  if (!spec) {
    return (
      <div className="chart-shell">
        <div className="chart-canvas" style={{ display: "grid", placeItems: "center" }}>
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
            paper_bgcolor: "rgba(0,0,0,0)",
            plot_bgcolor: "rgba(0,0,0,0)",
            font: { family: "inherit", size: 12 },
          }}
          config={{ responsive: true, displayModeBar: false }}
          style={{ width: "100%", minHeight: 280 }}
          useResizeHandler={true}
        />
      </div>
    </div>
  );
}
