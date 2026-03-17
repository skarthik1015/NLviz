"use client";

import { useState } from "react";
import { useConnection } from "../lib/connection-context";
import { deleteConnection } from "../lib/api";
import type { ConnectorType, ConnectionProfile } from "../lib/types";
import { PostgresForm } from "./connector-forms/postgres-form";
import { AthenaForm } from "./connector-forms/athena-form";
import { UploadForm } from "./connector-forms/upload-form";
import { SchemaGenerationStatus } from "./schema-generation-status";

type ConnectorCard = {
  type: ConnectorType | "upload";
  label: string;
  description: string;
  icon: string;
  enabled: boolean;
};

const CONNECTOR_CARDS: ConnectorCard[] = [
  { type: "postgres", label: "PostgreSQL", description: "Connect to a PostgreSQL database", icon: "🐘", enabled: true },
  { type: "athena", label: "S3 / Athena", description: "Query data in S3 via Amazon Athena", icon: "☁️", enabled: true },
  { type: "upload", label: "CSV / Parquet", description: "Upload a file to query instantly", icon: "📄", enabled: true },
  { type: "duckdb", label: "DuckDB", description: "Connect to a local DuckDB file", icon: "🦆", enabled: false },
];

type WizardStep = "pick" | "form" | "generating";

export function ConnectionPicker() {
  const { connections, refreshConnections, setActiveConnection } = useConnection();
  const [step, setStep] = useState<WizardStep>("pick");
  const [selectedType, setSelectedType] = useState<ConnectorCard["type"] | null>(null);
  const [newConnectionId, setNewConnectionId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  function handleCardClick(card: ConnectorCard) {
    if (!card.enabled) return;
    setSelectedType(card.type);
    setStep("form");
  }

  function handleConnectionCreated(connectionId: string) {
    setNewConnectionId(connectionId);
    setStep("generating");
  }

  function handleSchemaPublished(connectionId: string) {
    refreshConnections();
    setActiveConnection(connectionId);
  }

  function handleBack() {
    setStep("pick");
    setSelectedType(null);
    setNewConnectionId(null);
  }

  async function handleDelete(profile: ConnectionProfile) {
    if (!confirm(`Delete connection "${profile.display_name}"?`)) return;
    setDeleting(profile.connection_id);
    try {
      await deleteConnection(profile.connection_id);
      await refreshConnections();
    } finally {
      setDeleting(null);
    }
  }

  // ── Schema generation step ────────────────────────────────────────
  if (step === "generating" && newConnectionId) {
    return (
      <section className="panel" style={{ maxWidth: 600, margin: "40px auto", padding: 32 }}>
        <SchemaGenerationStatus
          connectionId={newConnectionId}
          onPublished={handleSchemaPublished}
          onBack={handleBack}
        />
      </section>
    );
  }

  // ── Credential form step ──────────────────────────────────────────
  if (step === "form" && selectedType) {
    return (
      <section className="panel" style={{ maxWidth: 600, margin: "40px auto", padding: 32 }}>
        <button
          type="button"
          onClick={handleBack}
          style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", marginBottom: 16, fontSize: "0.9rem" }}
        >
          ← Back to connectors
        </button>
        {selectedType === "postgres" && <PostgresForm onCreated={handleConnectionCreated} />}
        {selectedType === "athena" && <AthenaForm onCreated={handleConnectionCreated} />}
        {selectedType === "upload" && <UploadForm onCreated={handleConnectionCreated} />}
      </section>
    );
  }

  // ── Picker grid (default) ─────────────────────────────────────────
  return (
    <section style={{ maxWidth: 720, margin: "40px auto", padding: "0 16px" }}>
      <h1 style={{ fontSize: "1.6rem", fontWeight: 700, marginBottom: 8 }}>Connect to your data</h1>
      <p style={{ color: "var(--muted)", marginBottom: 32, fontSize: "0.95rem" }}>
        Choose a data source to get started. We&apos;ll auto-generate a semantic schema from your database structure.
      </p>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 16, marginBottom: 40 }}>
        {CONNECTOR_CARDS.map((card) => (
          <button
            key={card.type}
            onClick={() => handleCardClick(card)}
            disabled={!card.enabled}
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 8,
              padding: "24px 16px",
              borderRadius: 12,
              border: "1px solid var(--border, #e0e0e0)",
              background: card.enabled ? "var(--surface, #fff)" : "var(--surface-dim, #f5f5f5)",
              cursor: card.enabled ? "pointer" : "not-allowed",
              opacity: card.enabled ? 1 : 0.5,
              transition: "box-shadow 0.15s, border-color 0.15s",
            }}
            onMouseEnter={(e) => {
              if (card.enabled) e.currentTarget.style.borderColor = "var(--accent, #2563eb)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.borderColor = "var(--border, #e0e0e0)";
            }}
          >
            <span style={{ fontSize: "2rem" }}>{card.icon}</span>
            <span style={{ fontWeight: 600, fontSize: "0.95rem" }}>{card.label}</span>
            <span style={{ fontSize: "0.78rem", color: "var(--muted)", textAlign: "center" }}>
              {card.enabled ? card.description : "Coming soon"}
            </span>
          </button>
        ))}
      </div>

      {/* Existing connections */}
      {connections.length > 0 && (
        <>
          <h2 style={{ fontSize: "1.1rem", fontWeight: 600, marginBottom: 12 }}>Your connections</h2>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {connections.map((conn) => (
              <div
                key={conn.connection_id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  padding: "12px 16px",
                  borderRadius: 8,
                  border: "1px solid var(--border, #e0e0e0)",
                  background: "var(--surface, #fff)",
                }}
              >
                <div>
                  <span style={{ fontWeight: 600 }}>{conn.display_name}</span>
                  <span
                    style={{
                      marginLeft: 8,
                      padding: "2px 8px",
                      borderRadius: 4,
                      fontSize: "0.75rem",
                      background: "var(--surface-dim, #f0f0f0)",
                      color: "var(--muted)",
                    }}
                  >
                    {conn.connector_type}
                  </span>
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    className="button"
                    onClick={() => setActiveConnection(conn.connection_id)}
                    style={{ fontSize: "0.85rem", padding: "6px 16px" }}
                  >
                    Connect
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDelete(conn)}
                    disabled={deleting === conn.connection_id}
                    style={{
                      fontSize: "0.85rem",
                      padding: "6px 12px",
                      background: "none",
                      border: "1px solid var(--border, #e0e0e0)",
                      borderRadius: 6,
                      cursor: "pointer",
                      color: "var(--muted)",
                    }}
                  >
                    {deleting === conn.connection_id ? "..." : "Delete"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
