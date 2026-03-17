"use client";

import { useState, type FormEvent } from "react";
import { testConnection, createConnection } from "../../lib/api";
import type { ConnectionTestResponse } from "../../lib/types";

type Props = { onCreated: (connectionId: string) => void };

export function PostgresForm({ onCreated }: Props) {
  const [host, setHost] = useState("");
  const [port, setPort] = useState("5432");
  const [dbname, setDbname] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [schema, setSchema] = useState("public");
  const [displayName, setDisplayName] = useState("");

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ConnectionTestResponse | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const params = { host, port: parseInt(port), dbname, user, password, schema };

  async function handleTest(e: FormEvent) {
    e.preventDefault();
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      const result = await testConnection({ connector_type: "postgres", params });
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
        connector_type: "postgres",
        params,
        display_name: displayName || `${dbname}@${host}`,
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
      <h2 style={{ fontSize: "1.2rem", fontWeight: 700, marginBottom: 20 }}>🐘 PostgreSQL Connection</h2>
      <form onSubmit={handleTest}>
        <label style={labelStyle}>
          Host
          <input style={inputStyle} value={host} onChange={(e) => setHost(e.target.value)} placeholder="db.example.com" required />
        </label>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <label style={labelStyle}>
            Port
            <input style={inputStyle} value={port} onChange={(e) => setPort(e.target.value)} placeholder="5432" />
          </label>
          <label style={labelStyle}>
            Schema
            <input style={inputStyle} value={schema} onChange={(e) => setSchema(e.target.value)} placeholder="public" />
          </label>
        </div>
        <label style={labelStyle}>
          Database Name
          <input style={inputStyle} value={dbname} onChange={(e) => setDbname(e.target.value)} placeholder="mydb" required />
        </label>
        <label style={labelStyle}>
          Username
          <input style={inputStyle} value={user} onChange={(e) => setUser(e.target.value)} placeholder="postgres" required />
        </label>
        <label style={labelStyle}>
          Password
          <input style={inputStyle} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <label style={labelStyle}>
          Display Name
          <input style={inputStyle} value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="My Postgres DB" />
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
