import React from "react";
import { PlotSpec } from "../lib/types";

type PlotPreviewProps = {
  spec: PlotSpec;
};

function StatView({ spec }: PlotPreviewProps) {
  const value = spec.series[0]?.value ?? 0;
  return (
    <div className="chart-shell">
      <div className="chart-canvas" style={{ display: "grid", placeItems: "center" }}>
        <div style={{ textAlign: "center" }}>
          <div style={{ fontSize: "0.88rem", color: "var(--muted)", marginBottom: 8 }}>{spec.title}</div>
          <div style={{ fontSize: "3rem", lineHeight: 1, color: "var(--accent)" }}>
            {value > 10000 ? value.toLocaleString("en-US", { maximumFractionDigits: 0 }) : value.toFixed(2)}
          </div>
        </div>
      </div>
      <div className="chart-caption">Single-value query rendered as a stat card.</div>
    </div>
  );
}

function BarView({ spec }: PlotPreviewProps) {
  const data = spec.series.slice(0, 20);
  const maxValue = Math.max(...data.map((item) => item.value), 1);
  const barW = 34;
  const gap = 8;
  const chartH = 230;
  const barMaxH = 155;
  const totalW = Math.max(data.length * (barW + gap) + 40, 400);
  return (
    <div className="chart-shell">
      <div className="chart-canvas" style={{ overflowX: "auto" }}>
        <svg viewBox={`0 0 ${totalW} ${chartH}`} width="100%" height={chartH} role="img" aria-label={spec.title}>
          {data.map((item, index) => {
            const x = 20 + index * (barW + gap);
            const barHeight = Math.max((item.value / maxValue) * barMaxH, 2);
            const y = chartH - 46 - barHeight;
            const lx = x + barW / 2;
            const ly = chartH - 6;
            return (
              <g key={item.label + index}>
                <rect x={x} y={y} width={barW} height={barHeight} rx={5} fill="#b5532e" opacity="0.82" />
                <title>{item.label + ": " + item.value.toLocaleString()}</title>
                <text
                  x={lx}
                  y={ly}
                  textAnchor="end"
                  fontSize="9"
                  fill="#6f5c49"
                  transform={`rotate(-40,${lx},${ly})`}
                >
                  {item.label.slice(0, 16)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <div className="chart-caption">Top {data.length} results. Hover bars for exact values.</div>
    </div>
  );
}

function LineView({ spec }: PlotPreviewProps) {
  const maxValue = Math.max(...spec.series.map((item) => item.value), 1);
  const minValue = Math.min(...spec.series.map((item) => item.value), 0);
  const valueSpan = Math.max(maxValue - minValue, 1);
  const points = spec.series.slice(0, 12).map((item, index, items) => {
    const x = 30 + (index * 460) / Math.max(items.length - 1, 1);
    const y = 180 - ((item.value - minValue) / valueSpan) * 140;
    return { ...item, x, y };
  });

  return (
    <div className="chart-shell">
      <div className="chart-canvas">
        <svg viewBox="0 0 520 220" width="100%" height="100%" role="img" aria-label={spec.title}>
          <polyline
            fill="none"
            stroke="#1f6d57"
            strokeWidth="3"
            points={points.map((point) => `${point.x},${point.y}`).join(" ")}
          />
          {points.map((point, index) => (
            <g key={`${point.label}-${index}`}>
              <circle cx={point.x} cy={point.y} r="4" fill="#1f6d57" />
            </g>
          ))}
        </svg>
      </div>
      <div className="chart-caption">Line preview generated because the response includes a time dimension.</div>
    </div>
  );
}

export function PlotPreview({ spec }: PlotPreviewProps) {
  if (spec.chart_type === "stat") {
    return <StatView spec={spec} />;
  }

  if (spec.chart_type === "line") {
    return <LineView spec={spec} />;
  }

  return <BarView spec={spec} />;
}
