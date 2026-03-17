"use client";

import { useEffect, useState } from "react";
import { getMe } from "../lib/api";
import { useConnection } from "../lib/connection-context";
import type { User } from "../lib/types";

export function AppHeader() {
  const { mode, setMode, activeConnectionId, setActiveConnection, connections } = useConnection();
  const [user, setUser] = useState<User | null>(null);
  const [showDropdown, setShowDropdown] = useState(false);

  const activeConnection = connections.find((c) => c.connection_id === activeConnectionId);

  useEffect(() => {
    getMe().then(setUser).catch(() => {});
  }, []);

  return (
    <header
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "10px 20px",
        borderBottom: "1px solid var(--border, #e0e0e0)",
        background: "var(--surface, #fff)",
        marginBottom: 4,
        fontSize: "0.9rem",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <span style={{ fontWeight: 700, fontSize: "1rem" }}>NL Query Tool</span>

        {mode === "workspace" && activeConnection && (
          <div style={{ position: "relative" }}>
            <button
              onClick={() => setShowDropdown(!showDropdown)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 12px",
                borderRadius: 6,
                border: "1px solid var(--border, #e0e0e0)",
                background: "var(--surface-dim, #f5f5f5)",
                cursor: "pointer",
                fontSize: "0.85rem",
              }}
            >
              <span style={{ fontWeight: 600 }}>{activeConnection.display_name}</span>
              <span
                style={{
                  padding: "1px 6px",
                  borderRadius: 4,
                  fontSize: "0.72rem",
                  background: "var(--accent, #2563eb)",
                  color: "#fff",
                }}
              >
                {activeConnection.connector_type}
              </span>
              <span style={{ fontSize: "0.7rem" }}>▼</span>
            </button>

            {showDropdown && (
              <div
                style={{
                  position: "absolute",
                  top: "100%",
                  left: 0,
                  marginTop: 4,
                  minWidth: 220,
                  background: "var(--surface, #fff)",
                  border: "1px solid var(--border, #e0e0e0)",
                  borderRadius: 8,
                  boxShadow: "0 4px 12px rgba(0,0,0,0.1)",
                  zIndex: 100,
                  overflow: "hidden",
                }}
              >
                {connections.map((conn) => (
                  <button
                    key={conn.connection_id}
                    onClick={() => {
                      setActiveConnection(conn.connection_id);
                      setShowDropdown(false);
                    }}
                    style={{
                      display: "block",
                      width: "100%",
                      padding: "10px 14px",
                      border: "none",
                      background: conn.connection_id === activeConnectionId ? "var(--surface-dim, #f0f0f0)" : "transparent",
                      cursor: "pointer",
                      textAlign: "left",
                      fontSize: "0.85rem",
                    }}
                  >
                    <span style={{ fontWeight: 500 }}>{conn.display_name}</span>
                    <span style={{ marginLeft: 6, color: "var(--muted)", fontSize: "0.75rem" }}>{conn.connector_type}</span>
                  </button>
                ))}
                <div style={{ borderTop: "1px solid var(--border, #e0e0e0)" }}>
                  <button
                    onClick={() => {
                      setMode("picker");
                      setShowDropdown(false);
                    }}
                    style={{
                      display: "block",
                      width: "100%",
                      padding: "10px 14px",
                      border: "none",
                      background: "transparent",
                      cursor: "pointer",
                      textAlign: "left",
                      fontSize: "0.85rem",
                      color: "var(--accent, #2563eb)",
                      fontWeight: 500,
                    }}
                  >
                    + New Connection
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        {mode === "workspace" && (
          <button
            onClick={() => setMode("picker")}
            style={{
              padding: "6px 14px",
              borderRadius: 6,
              border: "1px solid var(--border, #e0e0e0)",
              background: "transparent",
              cursor: "pointer",
              fontSize: "0.85rem",
            }}
          >
            + New Connection
          </button>
        )}
        {user?.email && (
          <span style={{ color: "var(--muted)", fontSize: "0.82rem" }}>{user.email}</span>
        )}
      </div>
    </header>
  );
}
