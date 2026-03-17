"use client";

import { useEffect, useRef, useState } from "react";
import { generateSchema, getJobStatus, publishSchema } from "../lib/api";
import type { JobStatusResponse } from "../lib/types";

type Props = {
  connectionId: string;
  onPublished: (connectionId: string) => void;
  onBack: () => void;
};

export function SchemaGenerationStatus({ connectionId, onPublished, onBack }: Props) {
  const [status, setStatus] = useState<JobStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const startedRef = useRef(false);

  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;

    let cancelled = false;

    (async () => {
      try {
        // 1. Kick off generation
        const gen = await generateSchema(connectionId);
        const jobId = gen.job_id;

        // 2. Poll for completion
        const poll = async () => {
          if (cancelled) return;
          try {
            const job = await getJobStatus(connectionId, jobId);
            setStatus(job);
            if (job.status === "queued" || job.status === "running") {
              setTimeout(poll, 3000);
            } else if (job.status === "failed") {
              setError(job.error ?? "Schema generation failed");
            }
          } catch (err) {
            if (!cancelled) setError(err instanceof Error ? err.message : "Failed to check status");
          }
        };
        poll();
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to start schema generation");
      }
    })();

    return () => { cancelled = true; };
  }, [connectionId]);

  async function handlePublish() {
    if (!status?.schema_version_id) return;
    setPublishing(true);
    setError(null);
    try {
      await publishSchema(connectionId, status.schema_version_id);
      onPublished(connectionId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to publish schema");
    } finally {
      setPublishing(false);
    }
  }

  const isRunning = !status || status.status === "queued" || status.status === "running";
  const succeeded = status?.status === "succeeded";
  const failed = status?.status === "failed";

  return (
    <div>
      <h2 style={{ fontSize: "1.2rem", fontWeight: 700, marginBottom: 20 }}>Generating Semantic Schema</h2>

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
        {isRunning && (
          <>
            <div className="spinner" style={{
              width: 20, height: 20,
              border: "3px solid var(--border, #e0e0e0)",
              borderTopColor: "var(--accent, #2563eb)",
              borderRadius: "50%",
              animation: "spin 0.8s linear infinite",
            }} />
            <span style={{ color: "var(--muted)" }}>
              {status?.status === "running" ? "Analyzing your database schema..." : "Queued..."}
            </span>
          </>
        )}
        {succeeded && <span style={{ color: "#16a34a", fontWeight: 600 }}>Schema generated successfully!</span>}
        {failed && <span style={{ color: "#dc2626", fontWeight: 600 }}>Generation failed</span>}
      </div>

      {succeeded && status?.validation_summary && (
        <div style={{
          padding: 16, borderRadius: 8,
          background: "var(--surface-dim, #f0fdf4)",
          border: "1px solid #86efac",
          marginBottom: 20,
        }}>
          <p style={{ fontWeight: 600, marginBottom: 8 }}>Validation Summary</p>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, fontSize: "0.85rem" }}>
            <div>Metrics: {status.validation_summary.valid_metrics} / {status.validation_summary.total_metrics}</div>
            <div>Dimensions: {status.validation_summary.valid_dimensions} / {status.validation_summary.total_dimensions}</div>
            <div style={{ gridColumn: "span 2" }}>
              Confidence: {(status.validation_summary.confidence_score * 100).toFixed(0)}%
            </div>
          </div>
          {status.validation_summary.broken_metrics.length > 0 && (
            <p style={{ fontSize: "0.8rem", color: "var(--muted)", marginTop: 8 }}>
              Broken metrics: {status.validation_summary.broken_metrics.join(", ")}
            </p>
          )}
        </div>
      )}

      {error && <div className="error" style={{ marginBottom: 16 }}>{error}</div>}

      <div style={{ display: "flex", gap: 12 }}>
        {succeeded && (
          <button className="button" onClick={handlePublish} disabled={publishing}>
            {publishing ? "Publishing..." : "Publish & Start Querying"}
          </button>
        )}
        <button
          type="button"
          onClick={onBack}
          style={{
            background: "none",
            border: "1px solid var(--border, #e0e0e0)",
            borderRadius: 6,
            padding: "8px 16px",
            cursor: "pointer",
            fontSize: "0.9rem",
          }}
        >
          Back
        </button>
      </div>

      <style>{`
        @keyframes spin {
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
