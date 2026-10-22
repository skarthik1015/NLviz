"use client";

import { useState, type FormEvent } from "react";
import { importFromS3 } from "../../lib/api";
import { creationNotices } from "./database-form";

type Props = { onCreated: (connectionId: string) => void };

export function S3ImportForm({ onCreated }: Props) {
  const [s3Url, setS3Url] = useState("");
  const [region, setRegion] = useState("us-east-1");
  const [accessKeyId, setAccessKeyId] = useState("");
  const [secretAccessKey, setSecretAccessKey] = useState("");
  const [sessionToken, setSessionToken] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<{ id: string; notices: string[] } | null>(null);

  async function handleImport(e: FormEvent) {
    e.preventDefault();
    setImporting(true);
    setError(null);
    try {
      const result = await importFromS3({
        s3_url: s3Url.trim(),
        display_name: displayName || s3Url.split("/").pop() || "S3 import",
        region,
        access_key_id: accessKeyId || null,
        secret_access_key: secretAccessKey || null,
        session_token: sessionToken || null,
      });
      const notices = creationNotices(result);
      if (notices.length) {
        setCreated({ id: result.connection_id, notices });
      } else {
        onCreated(result.connection_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setImporting(false);
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
  const labelStyle = { display: "block", marginBottom: 12, fontSize: "0.85rem", fontWeight: 500 as const };

  if (created) {
    return (
      <div>
        <h2 style={{ fontSize: "1.2rem", fontWeight: 700, marginBottom: 16 }}>Imported</h2>
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
      <h2 style={{ fontSize: "1.2rem", fontWeight: 700, marginBottom: 8 }}>☁️ Import a file from S3</h2>
      <p style={{ fontSize: "0.8rem", opacity: 0.7, marginBottom: 20 }}>
        One CSV or Parquet object is copied in. Leave the keys empty for a public bucket. Keys are used
        once for the download and are not stored.
      </p>
      <form onSubmit={handleImport}>
        <label style={labelStyle}>
          Object URL
          <input style={inputStyle} value={s3Url} onChange={(e) => setS3Url(e.target.value)} placeholder="s3://my-bucket/exports/sales.parquet" required />
        </label>
        <label style={labelStyle}>
          Region
          <input style={inputStyle} value={region} onChange={(e) => setRegion(e.target.value)} placeholder="us-east-1" />
        </label>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <label style={labelStyle}>
            Access key ID
            <input style={inputStyle} value={accessKeyId} onChange={(e) => setAccessKeyId(e.target.value)} placeholder="optional" autoComplete="off" />
          </label>
          <label style={labelStyle}>
            Secret access key
            <input style={inputStyle} type="password" value={secretAccessKey} onChange={(e) => setSecretAccessKey(e.target.value)} placeholder="optional" autoComplete="off" />
          </label>
        </div>
        <label style={labelStyle}>
          Session token
          <input style={inputStyle} type="password" value={sessionToken} onChange={(e) => setSessionToken(e.target.value)} placeholder="only for temporary credentials" autoComplete="off" />
        </label>
        <label style={labelStyle}>
          Display Name
          <input style={inputStyle} value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="Sales export" />
        </label>
        <button className="button" type="submit" disabled={importing} style={{ marginTop: 8 }}>
          {importing ? "Importing..." : "Import & Generate Schema"}
        </button>
      </form>
      {error && <div className="error" style={{ marginTop: 12 }}>{error}</div>}
    </div>
  );
}
