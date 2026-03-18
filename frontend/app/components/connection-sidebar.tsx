"use client";

import { useState } from "react";
import { Database, Plus, ChevronLeft, ChevronRight, Check } from "lucide-react";
import { useConnection } from "../lib/connection-context";

export function ConnectionSidebar() {
  const { connections, activeConnectionId, setActiveConnection, setMode } = useConnection();
  const [collapsed, setCollapsed] = useState(false);

  return (
    <aside className={`sidebar${collapsed ? " collapsed" : ""}`}>
      {/* Toggle button */}
      <div className="sidebar-toggle-row">
        {!collapsed && (
          <span className="sidebar-heading">Connections</span>
        )}
        <button
          className="sidebar-toggle-btn"
          onClick={() => setCollapsed(!collapsed)}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? (
            <ChevronRight className="h-4 w-4" />
          ) : (
            <ChevronLeft className="h-4 w-4" />
          )}
        </button>
      </div>

      {/* New Connection — pinned at top */}
      <div className="sidebar-top-action">
        <button
          className="sidebar-new-btn"
          onClick={() => setMode("picker")}
          title="New Connection"
        >
          <Plus className="h-4 w-4 shrink-0" />
          {!collapsed && <span>New Connection</span>}
        </button>
      </div>

      {/* Connection list */}
      <nav className="sidebar-nav">
        {connections.map((conn) => {
          const isActive = conn.connection_id === activeConnectionId;
          const isReady = conn.query_ready;
          return (
            <button
              key={conn.connection_id}
              onClick={() => {
                if (!isReady) return;
                setActiveConnection(conn.connection_id);
              }}
              disabled={!isReady}
              title={conn.display_name}
              className={[
                "sidebar-item",
                isActive ? "active" : "",
                !isReady ? "disabled" : "",
              ]
                .filter(Boolean)
                .join(" ")}
            >
              <Database className="sidebar-item-icon" />
              {!collapsed && (
                <div className="sidebar-item-text">
                  <span className="sidebar-item-name">{conn.display_name}</span>
                  <span className="sidebar-item-sub">
                    {isReady ? conn.connector_type : "setup required"}
                  </span>
                </div>
              )}
              {!collapsed && isActive && (
                <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
              )}
            </button>
          );
        })}
      </nav>

    </aside>
  );
}
