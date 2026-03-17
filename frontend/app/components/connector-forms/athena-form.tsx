"use client";

import { useState, type FormEvent } from "react";
import { testConnection, createConnection } from "../../lib/api";
import type { ConnectionTestResponse } from "../../lib/types";

type Props = { onCreated: (connectionId: string) => void };

export function AthenaForm({ onCreated }: Props) {
  const [accessKeyId, setAccessKeyId] = useState("");
  const [secretAccessKey, setSecretAccessKey] = useState("");
  const [region, setRegion] = useState("us-east-1");
  const [databaseName, setDatabaseName] = useState("");
  const [s3StagingDir, setS3StagingDir] = useState("");
  const [workGroup, setWorkGroup] = useState("primary");
  const [displayName, setDisplayName] = useState("");

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ConnectionTestResponse | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const params = {
    aws_access_key_id: accessKeyId,
    aws_secret_access_key: secretAccessKey,
    region_name: region,
    database_name: databaseName,
    s3_staging_dir: s3StagingDir,
    work_group: workGroup,
  };

  async function handleTest(e: FormEvent) {
    e.preventDefault();
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      const result = await testConnection({ connector_type: "athena", params });
      setTestResult(result);
      if (!result.success) setError(result.error ?? "Connection test failed");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Test failed");
    } finally {
      setTesting(false);
    }
  }

  async function handleCreate() {
    setCreating(true);
    setError(null);
    try {
      const result = await createConnection({
        connector_type: "athena",
        params,
        display_name: displayName || `Athena: ${databaseName}`,
      });
      onCreated(result.connection_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create connection");
    } finally {
      setCreating(false);
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

  return (
    <div>
      <h2 style={{ fontSize: "1.2rem", fontWeight: 700, marginBottom: 20 }}>☁️ S3 / Athena Connection</h2>
      <form onSubmit={handleTest}>
        <label style={labelStyle}>
          AWS Access Key ID
          <input style={inputStyle} value={accessKeyId} onChange={(e) => setAccessKeyId(e.target.value)} placeholder="AKIA..." required />
        </label>
        <label style={labelStyle}>
          AWS Secret Access Key
          <input style={inputStyle} type="password" value={secretAccessKey} onChange={(e) => setSecretAccessKey(e.target.value)} required />
        </label>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <label style={labelStyle}>
            Region
            <input style={inputStyle} value={region} onChange={(e) => setRegion(e.target.value)} placeholder="us-east-1" />
          </label>
          <label style={labelStyle}>
            Workgroup
            <input style={inputStyle} value={workGroup} onChange={(e) => setWorkGroup(e.target.value)} placeholder="primary" />
          </label>
        </div>
        <label style={labelStyle}>
          Athena Database Name
          <input style={inputStyle} value={databaseName} onChange={(e) => setDatabaseName(e.target.value)} placeholder="my_database" required />
        </label>
        <label style={labelStyle}>
          S3 Output Location
          <input style={inputStyle} value={s3StagingDir} onChange={(e) => setS3StagingDir(e.target.value)} placeholder="s3://my-bucket/athena-results/" required />
        </label>
        <label style={labelStyle}>
          Display Name
          <input style={inputStyle} value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="My S3 Data Lake" />
        </label>

        <div style={{ display: "flex", gap: 12, marginTop: 16 }}>
          <button className="button" type="submit" disabled={testing}>
            {testing ? "Testing..." : "Test Connection"}
          </button>
          {testResult?.success && (
            <button className="button" type="button" onClick={handleCreate} disabled={creating}>
              {creating ? "Creating..." : "Create & Generate Schema"}
            </button>
          )}
        </div>
      </form>

      {testResult?.success && (
        <div style={{ marginTop: 16, padding: 12, borderRadius: 8, background: "var(--surface-dim, #f0fdf4)", border: "1px solid #86efac" }}>
          <strong>Connection successful!</strong> Found {testResult.tables.length} table(s):
          <ul style={{ margin: "8px 0 0", paddingLeft: 20, fontSize: "0.85rem" }}>
            {testResult.tables.slice(0, 10).map((t) => (
              <li key={t.name}>
                {t.name} — {t.row_count.toLocaleString()} rows, {t.column_count} columns
              </li>
            ))}
            {testResult.tables.length > 10 && <li>... and {testResult.tables.length - 10} more</li>}
          </ul>
        </div>
      )}

      {error && <div className="error" style={{ marginTop: 12 }}>{error}</div>}
    </div>
  );
}
