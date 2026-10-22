"use client";

import { useState, type FormEvent } from "react";
import { testConnection, createConnection } from "../../lib/api";
import type { ConnectionCreateResponse, ConnectionTestResponse } from "../../lib/types";

export type DatabaseKind = "postgres" | "mysql";

type Props = {
  kind: DatabaseKind;
  onCreated: (connectionId: string) => void;
};

const KINDS: Record<DatabaseKind, { title: string; port: string; user: string; note: string }> = {
  postgres: {
    title: "🐘 PostgreSQL Connection",
    port: "5432",
    user: "postgres",
    note: "Queried live through Jammi. Tables in the 'public' schema are available (views are not yet).",
  },
  mysql: {
    title: "🐬 MySQL Connection",
    port: "3306",
    user: "root",
    note: "The tables are copied into Jammi when you connect (a snapshot), so answers are exact; reconnect to refresh.",
  },
};

/** Tables Jammi could not register, and columns renamed to queryable identifiers. */
export function creationNotices(result: ConnectionCreateResponse): string[] {
  const notices: string[] = [...(result.notes ?? [])];
  if (result.skipped_tables?.length) {
    notices.push(`Skipped (the query engine cannot address these tables): ${result.skipped_tables.join(", ")}`);
  }
  const renamed = Object.entries(result.renamed_columns ?? {});
  if (renamed.length) {
    notices.push(`Renamed columns: ${renamed.map(([from, to]) => `${from} → ${to}`).join(", ")}`);
  }
  return notices;
}

export function DatabaseForm({ kind, onCreated }: Props) {
  const config = KINDS[kind];
  const [host, setHost] = useState("");
  const [port, setPort] = useState(config.port);
  const [dbname, setDbname] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ConnectionTestResponse | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<{ id: string; notices: string[] } | null>(null);

  const params = { host, port: parseInt(port), dbname, user, password };

  async function handleTest(e: FormEvent) {
    e.preventDefault();
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      const result = await testConnection({ connector_type: kind, params });
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
        connector_type: kind,
        params,
        display_name: displayName || `${dbname}@${host}`,
      });
      const notices = creationNotices(result);
      if (notices.length) {
        setCreated({ id: result.connection_id, notices });  // let the user read them first
      } else {
        onCreated(result.connection_id);
      }
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

  if (created) {
    return (
      <div>
        <h2 style={{ fontSize: "1.2rem", fontWeight: 700, marginBottom: 16 }}>Connected</h2>
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
      <h2 style={{ fontSize: "1.2rem", fontWeight: 700, marginBottom: 8 }}>{config.title}</h2>
      <p style={{ fontSize: "0.8rem", opacity: 0.7, marginBottom: 20 }}>{config.note}</p>
      <form onSubmit={handleTest}>
        <div style={{ display: "grid", gridTemplateColumns: "3fr 1fr", gap: 12 }}>
          <label style={labelStyle}>
            Host
            <input style={inputStyle} value={host} onChange={(e) => setHost(e.target.value)} placeholder="db.example.com" required />
          </label>
          <label style={labelStyle}>
            Port
            <input style={inputStyle} value={port} onChange={(e) => setPort(e.target.value)} placeholder={config.port} />
          </label>
        </div>
        <label style={labelStyle}>
          Database Name
          <input style={inputStyle} value={dbname} onChange={(e) => setDbname(e.target.value)} placeholder="mydb" required />
        </label>
        <label style={labelStyle}>
          Username
          <input style={inputStyle} value={user} onChange={(e) => setUser(e.target.value)} placeholder={config.user} required />
        </label>
        <label style={labelStyle}>
          Password
          <input style={inputStyle} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <label style={labelStyle}>
          Display Name
          <input style={inputStyle} value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="My database" />
        </label>

        <div style={{ display: "flex", gap: 12, marginTop: 16 }}>
          <button className="button" type="submit" disabled={testing}>
            {testing ? "Testing..." : "Test Connection"}
          </button>
          {testResult?.success && (
            <button className="button" type="button" onClick={handleCreate} disabled={creating}>
              {creating ? (kind === "mysql" ? "Importing..." : "Creating...") : "Create & Generate Schema"}
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
