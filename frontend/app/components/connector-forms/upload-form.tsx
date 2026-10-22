"use client";

import { useState, useRef, type DragEvent } from "react";
import { uploadFile } from "../../lib/api";
import { creationNotices } from "./database-form";

type Props = { onCreated: (connectionId: string) => void };

export function UploadForm({ onCreated }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [created, setCreated] = useState<{ id: string; notices: string[] } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  function handleFileChange(selected: File | null) {
    if (!selected) return;
    const ext = selected.name.split(".").pop()?.toLowerCase();
    if (ext !== "csv" && ext !== "xlsx" && ext !== "parquet") {
      setError("Only CSV, Excel (.xlsx) and Parquet files are supported");
      return;
    }
    setFile(selected);
    setError(null);
    if (!displayName) {
      setDisplayName(selected.name.replace(/\.(csv|xlsx|parquet)$/i, ""));
    }
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const dropped = e.dataTransfer.files[0];
    if (dropped) handleFileChange(dropped);
  }

  async function handleUpload() {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const result = await uploadFile(file, displayName || file.name);
      const notices = creationNotices(result);
      if (notices.length) {
        setCreated({ id: result.connection_id, notices });  // let the user read them first
      } else {
        onCreated(result.connection_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  const inputStyle = {
    width: "100%",
    padding: "8px 12px",
    borderRadius: 6,
    border: "1px solid var(--border, #e0e0e0)",
    fontSize: "0.9rem",
    background: "var(--surface, #fff)",
  };

  if (created) {
    return (
      <div>
        <h2 style={{ fontSize: "1.2rem", fontWeight: 700, marginBottom: 16 }}>Uploaded</h2>
        <ul style={{ margin: "0 0 16px", paddingLeft: 20, fontSize: "0.85rem" }}>
          {created.notices.map((notice) => (
            <li key={notice}>{notice}</li>
          ))}
        </ul>
        <button className="button" type="button" onClick={() => onCreated(created.id)}>
          Continue to schema generation
        </button>
      </div>
    );
  }

  return (
    <div>
      <h2 style={{ fontSize: "1.2rem", fontWeight: 700, marginBottom: 20 }}>📄 Upload CSV / Parquet</h2>

      <div
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        style={{
          border: `2px dashed ${dragOver ? "var(--accent, #2563eb)" : "var(--border, #d0d0d0)"}`,
          borderRadius: 12,
          padding: "40px 20px",
          textAlign: "center",
          cursor: "pointer",
          background: dragOver ? "var(--surface-dim, #f0f7ff)" : "transparent",
          transition: "border-color 0.15s, background 0.15s",
          marginBottom: 16,
        }}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".csv,.xlsx,.parquet"
          style={{ display: "none" }}
          onChange={(e) => handleFileChange(e.target.files?.[0] ?? null)}
        />
        {file ? (
          <p style={{ fontSize: "0.95rem" }}>
            <strong>{file.name}</strong> ({(file.size / 1024 / 1024).toFixed(1)} MB)
          </p>
        ) : (
          <p style={{ color: "var(--muted)", fontSize: "0.95rem" }}>
            Drop a CSV, Excel or Parquet file here, or click to browse
          </p>
        )}
      </div>

      <label style={{ display: "block", marginBottom: 12, fontSize: "0.85rem", fontWeight: 500 }}>
        Display Name
        <input style={inputStyle} value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="My Dataset" />
      </label>

      <button className="button" onClick={handleUpload} disabled={!file || uploading} style={{ marginTop: 8 }}>
        {uploading ? "Uploading..." : "Upload & Generate Schema"}
      </button>

      {error && <div className="error" style={{ marginTop: 12 }}>{error}</div>}
    </div>
  );
}
