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
          <div style={{ fontSize: "3rem", lineHeight: 1, color: "var(--accent)" }}>{value.toFixed(2)}</div>
        </div>
      </div>
      <div className="chart-caption">Single-value query rendered as a stat card.</div>
    </div>
  );
}

function BarView({ spec }: PlotPreviewProps) {
  const maxValue = Math.max(...spec.series.map((item) => item.value), 1);
  return (
    <div className="chart-shell">
      <div className="chart-canvas">
        <svg viewBox="0 0 520 220" width="100%" height="100%" role="img" aria-label={spec.title}>
          {spec.series.slice(0, 8).map((item, index) => {
            const width = 48;
            const gap = 16;
            const x = 36 + index * (width + gap);
            const barHeight = (item.value / maxValue) * 150;
            const y = 180 - barHeight;
            return (
              <g key={`${item.label}-${index}`}>
                <rect x={x} y={y} width={width} height={barHeight} rx={8} fill="#b5532e" opacity="0.86" />
                <text x={x + width / 2} y={198} textAnchor="middle" fontSize="10" fill="#6f5c49">
                  {item.label.slice(0, 10)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <div className="chart-caption">Bar preview generated from `metric_value` against the first dimension column.</div>
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
          {points.map((point) => (
            <g key={point.label}>
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
