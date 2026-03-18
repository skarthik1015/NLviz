"use client";

import { useState, type FormEvent } from "react";
import { testConnection, createConnection } from "../../lib/api";
import type { ConnectionTestResponse } from "../../lib/types";

type AuthMode = "task_role" | "iam_keys" | "role_arn";
type Props = { onCreated: (connectionId: string) => void };

export function AthenaForm({ onCreated }: Props) {
  const [authMode, setAuthMode] = useState<AuthMode>("task_role");

  // iam_keys fields
  const [accessKeyId, setAccessKeyId] = useState("");
  const [secretAccessKey, setSecretAccessKey] = useState("");
  const [sessionToken, setSessionToken] = useState("");

  // role_arn fields (skeleton — not yet active)
  const [roleArn, setRoleArn] = useState("");
  const [externalId, setExternalId] = useState("");

  // common fields
  const [region, setRegion] = useState("us-east-1");
  const [databaseName, setDatabaseName] = useState("");
  const [s3StagingDir, setS3StagingDir] = useState("");
  const [workGroup, setWorkGroup] = useState("primary");
  const [displayName, setDisplayName] = useState("");

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ConnectionTestResponse | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function buildParams() {
    const base = {
      auth_mode: authMode,
      region_name: region,
      database_name: databaseName,
      s3_staging_dir: s3StagingDir,
      work_group: workGroup,
    };
    if (authMode === "iam_keys") {
      return {
        ...base,
        aws_access_key_id: accessKeyId,
        aws_secret_access_key: secretAccessKey,
        ...(sessionToken ? { aws_session_token: sessionToken } : {}),
      };
    }
    if (authMode === "role_arn") {
      return { ...base, role_arn: roleArn, external_id: externalId };
    }
    return base; // task_role — no credentials
  }

  async function handleTest(e: FormEvent) {
    e.preventDefault();
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      const result = await testConnection({ connector_type: "athena", params: buildParams() });
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
        params: buildParams(),
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

  const AUTH_MODES: { id: AuthMode; label: string; sub: string; disabled?: boolean }[] = [
    {
      id: "task_role",
      label: "Fargate / EC2 Task Role",
      sub: "Recommended — uses the app's own IAM role, no credentials needed",
    },
    {
      id: "iam_keys",
      label: "IAM Access Keys",
      sub: "Long-term keys (AKIA…) or temporary STS keys (ASIA…) + session token",
    },
    {
      id: "role_arn",
      label: "Cross-Account Role ARN",
      sub: "Coming soon — for multi-tenant / external AWS accounts",
      disabled: true,
    },
  ];

  return (
    <div>
      <h2 style={{ fontSize: "1.2rem", fontWeight: 700, marginBottom: 20 }}>☁️ S3 / Athena Connection</h2>

      {/* Auth mode selector */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: 8 }}>Authentication Method</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {AUTH_MODES.map((mode) => (
            <label
              key={mode.id}
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: 10,
                padding: "10px 12px",
                borderRadius: 8,
                border: `1px solid ${authMode === mode.id ? "var(--primary, #7c3aed)" : "var(--border, #e0e0e0)"}`,
                background: authMode === mode.id ? "var(--primary-bg, #f5f3ff)" : "var(--surface, #fff)",
                cursor: mode.disabled ? "not-allowed" : "pointer",
                opacity: mode.disabled ? 0.5 : 1,
              }}
            >
              <input
                type="radio"
                name="auth_mode"
                value={mode.id}
                checked={authMode === mode.id}
                disabled={mode.disabled}
                onChange={() => setAuthMode(mode.id)}
                style={{ marginTop: 2 }}
              />
              <div>
                <div style={{ fontSize: "0.875rem", fontWeight: 600 }}>
                  {mode.label}
                  {mode.disabled && (
                    <span style={{ marginLeft: 8, fontSize: "0.7rem", background: "#e0e0e0", padding: "1px 6px", borderRadius: 4 }}>
                      COMING SOON
                    </span>
                  )}
                </div>
                <div style={{ fontSize: "0.78rem", color: "var(--fg-muted, #666)", marginTop: 2 }}>{mode.sub}</div>
              </div>
            </label>
          ))}
        </div>
      </div>

      <form onSubmit={handleTest}>
        {/* IAM keys fields */}
        {authMode === "iam_keys" && (
          <>
            <label style={labelStyle}>
              AWS Access Key ID
              <input style={inputStyle} value={accessKeyId} onChange={(e) => setAccessKeyId(e.target.value)} placeholder="AKIA… or ASIA…" required />
            </label>
            <label style={labelStyle}>
              AWS Secret Access Key
              <input style={inputStyle} type="password" value={secretAccessKey} onChange={(e) => setSecretAccessKey(e.target.value)} required />
            </label>
            <label style={labelStyle}>
              Session Token <span style={{ fontWeight: 400, color: "#888" }}>(required for ASIA/temporary credentials)</span>
              <input style={inputStyle} type="password" value={sessionToken} onChange={(e) => setSessionToken(e.target.value)} placeholder="Optional for AKIA keys" />
            </label>
          </>
        )}

        {/* Cross-account role fields (skeleton) */}
        {authMode === "role_arn" && (
          <>
            <label style={labelStyle}>
              Role ARN
              <input style={inputStyle} value={roleArn} onChange={(e) => setRoleArn(e.target.value)} placeholder="arn:aws:iam::123456789012:role/NLQueryToolAccess" disabled />
            </label>
            <label style={labelStyle}>
              External ID <span style={{ fontWeight: 400, color: "#888" }}>(optional)</span>
              <input style={inputStyle} value={externalId} onChange={(e) => setExternalId(e.target.value)} placeholder="Shared secret set in the trust policy" disabled />
            </label>
          </>
        )}

        {/* Task role info banner */}
        {authMode === "task_role" && (
          <div style={{ marginBottom: 16, padding: "10px 14px", borderRadius: 8, background: "#f0fdf4", border: "1px solid #86efac", fontSize: "0.83rem", color: "#166534" }}>
            The app will use its own Fargate task role to access Athena. Ensure the task role has
            <code style={{ margin: "0 4px", background: "#dcfce7", padding: "1px 4px", borderRadius: 3 }}>athena:*</code>,
            <code style={{ margin: "0 4px", background: "#dcfce7", padding: "1px 4px", borderRadius: 3 }}>s3:*</code> on your staging bucket, and
            <code style={{ margin: "0 4px", background: "#dcfce7", padding: "1px 4px", borderRadius: 3 }}>glue:Get*</code> permissions.
          </div>
        )}

        {/* Common fields */}
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
          <button className="button" type="submit" disabled={testing || authMode === "role_arn"}>
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
