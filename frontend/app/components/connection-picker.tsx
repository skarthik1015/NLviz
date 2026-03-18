"use client";

import { motion } from "framer-motion";
import { Cloud, Database, FileText, ChevronRight, Trash2, Settings } from "lucide-react";
import { useState } from "react";
import { useConnection } from "../lib/connection-context";
import { deleteConnection } from "../lib/api";
import type { ConnectorType, ConnectionProfile } from "../lib/types";
import { PostgresForm } from "./connector-forms/postgres-form";
import { AthenaForm } from "./connector-forms/athena-form";
import { UploadForm } from "./connector-forms/upload-form";
import { SchemaGenerationStatus } from "./schema-generation-status";
import { GlowyWavesHero } from "./ui/glowy-waves-hero";

type ConnectorCard = {
  type: ConnectorType | "upload";
  label: string;
  description: string;
  icon: React.ReactNode;
  enabled: boolean;
};

const CONNECTOR_CARDS: ConnectorCard[] = [
  {
    type: "postgres",
    label: "PostgreSQL",
    description: "Connect to a PostgreSQL database",
    icon: <Database className="h-6 w-6" />,
    enabled: true,
  },
  {
    type: "athena",
    label: "S3 / Athena",
    description: "Query data in S3 via Amazon Athena",
    icon: <Cloud className="h-6 w-6" />,
    enabled: true,
  },
  {
    type: "upload",
    label: "CSV / Parquet",
    description: "Upload a file to query instantly",
    icon: <FileText className="h-6 w-6" />,
    enabled: true,
  },
  {
    type: "duckdb",
    label: "DuckDB",
    description: "Coming soon",
    icon: <Database className="h-6 w-6 opacity-40" />,
    enabled: false,
  },
];

type WizardStep = "pick" | "form" | "generating";

const cardVariants = {
  hidden: { opacity: 0, y: 20 },
  visible: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: { delay: i * 0.08, duration: 0.5, ease: "easeOut" as const },
  }),
};

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
    void refreshConnections();
    setActiveConnection(connectionId);
  }

  function handleResumeGeneration(connectionId: string) {
    setNewConnectionId(connectionId);
    setStep("generating");
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

  // ── Schema generation step ──────────────────────────────────────
  if (step === "generating" && newConnectionId) {
    return (
      <div className="relative min-h-screen flex items-center justify-center px-4">
        <div
          className="relative z-10 w-full max-w-lg rounded-3xl border border-border/30 bg-background/95 p-8 backdrop-blur-md shadow-xl"
          style={{ boxShadow: "0 18px 50px rgba(58,34,12,0.18)" }}
        >
          <SchemaGenerationStatus
            connectionId={newConnectionId}
            onPublished={handleSchemaPublished}
            onBack={handleBack}
          />
        </div>
      </div>
    );
  }

  // ── Credential form step ────────────────────────────────────────
  if (step === "form" && selectedType) {
    return (
      <div className="relative min-h-screen flex items-center justify-center px-4 py-12">
        <div
          className="relative z-10 w-full max-w-lg rounded-3xl border border-border/30 bg-background/95 p-8 backdrop-blur-md shadow-xl"
          style={{ boxShadow: "0 18px 50px rgba(58,34,12,0.18)" }}
        >
          <button
            type="button"
            onClick={handleBack}
            className="mb-5 flex items-center gap-1.5 text-sm font-medium text-primary hover:opacity-80 transition-opacity"
          >
            ← Back to connectors
          </button>
          {selectedType === "postgres" && <PostgresForm onCreated={handleConnectionCreated} />}
          {selectedType === "athena" && <AthenaForm onCreated={handleConnectionCreated} />}
          {selectedType === "upload" && <UploadForm onCreated={handleConnectionCreated} />}
        </div>
      </div>
    );
  }

  // ── Picker grid (default) — hero canvas + glassmorphism overlay ──
  return (
    <div className="relative min-h-screen overflow-y-auto">
      {/* Animated canvas background */}
      <div className="fixed inset-0">
        <GlowyWavesHero onConnect={() => {}} canvasOnly />
      </div>

      {/* Content overlay */}
      <div className="relative z-10 flex min-h-screen flex-col items-center justify-center px-4 py-20">
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7 }}
          className="w-full max-w-2xl"
        >
          {/* Header card */}
          <div className="mb-8 rounded-2xl bg-background/90 p-6 text-center shadow-lg backdrop-blur-sm">
            <p className="mb-2 text-xs font-semibold uppercase tracking-widest text-primary">
              NL Query Tool
            </p>
            <h1
              className="mb-3 text-4xl font-bold text-foreground md:text-5xl"
              style={{ fontFamily: "Georgia, 'Times New Roman', serif" }}
            >
              Connect your data
            </h1>
            <p className="text-foreground/70">
              Choose a data source to get started. We&apos;ll auto-generate a semantic schema so you can query in plain English.
            </p>
          </div>

          {/* Connector cards */}
          <div className="mb-8 grid grid-cols-2 gap-4 sm:grid-cols-4">
            {CONNECTOR_CARDS.map((card, i) => (
              <motion.button
                key={card.type}
                custom={i}
                variants={cardVariants}
                initial="hidden"
                animate="visible"
                onClick={() => handleCardClick(card)}
                disabled={!card.enabled}
                whileHover={card.enabled ? { y: -4, scale: 1.02 } : {}}
                whileTap={card.enabled ? { scale: 0.97 } : {}}
                className={[
                  "flex flex-col items-center gap-3 rounded-2xl border p-5 text-center transition-all",
                  card.enabled
                    ? "cursor-pointer border-border/40 bg-background/90 backdrop-blur-sm hover:border-primary/40 hover:bg-background shadow-md hover:shadow-lg"
                    : "cursor-not-allowed border-border/20 bg-background/60 opacity-50",
                ].join(" ")}
              >
                <span className="text-primary">{card.icon}</span>
                <span className="text-sm font-semibold text-foreground">{card.label}</span>
                <span className="text-xs text-foreground/50 leading-tight">{card.description}</span>
                {card.enabled && (
                  <ChevronRight className="h-3.5 w-3.5 text-primary/60" />
                )}
              </motion.button>
            ))}
          </div>

          {/* Existing connections */}
          {connections.length > 0 && (
            <motion.div
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.35, duration: 0.5 }}
              className="rounded-2xl border border-border/30 bg-background/95 p-5 shadow-lg backdrop-blur-md"
            >
              <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-foreground/70">
                Your connections
              </h2>
              <div className="flex flex-col gap-2 max-h-[280px] overflow-y-auto pr-1">
                {connections.map((conn) => (
                  <div
                    key={conn.connection_id}
                    className="flex items-center justify-between gap-3 rounded-xl border border-border/30 bg-background/80 px-4 py-3"
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      <Database className="h-4 w-4 shrink-0 text-primary/70" />
                      <div className="min-w-0">
                        <span className="block truncate text-sm font-semibold text-foreground">
                          {conn.display_name}
                        </span>
                        <span className="text-xs text-foreground/50">{conn.connector_type}</span>
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <motion.button
                        whileTap={{ scale: 0.95 }}
                        onClick={() =>
                          conn.query_ready
                            ? setActiveConnection(conn.connection_id)
                            : handleResumeGeneration(conn.connection_id)
                        }
                        className="flex items-center gap-1.5 rounded-full bg-primary px-3.5 py-1.5 text-xs font-semibold text-primary-foreground transition-opacity hover:opacity-90"
                      >
                        {conn.query_ready ? (
                          <>Connect <ChevronRight className="h-3 w-3" /></>
                        ) : (
                          <>Setup <Settings className="h-3 w-3" /></>
                        )}
                      </motion.button>
                      <motion.button
                        whileTap={{ scale: 0.95 }}
                        onClick={() => handleDelete(conn)}
                        disabled={deleting === conn.connection_id}
                        className="rounded-full border border-border/30 p-1.5 text-foreground/40 transition-colors hover:border-destructive/40 hover:text-destructive disabled:opacity-40"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </motion.button>
                    </div>
                  </div>
                ))}
              </div>
            </motion.div>
          )}
        </motion.div>
      </div>
    </div>
  );
}
