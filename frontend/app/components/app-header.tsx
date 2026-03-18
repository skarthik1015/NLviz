"use client";

import { useEffect, useRef, useState } from "react";
import { Database, ChevronDown, Plus, Check } from "lucide-react";
import { getMe } from "../lib/api";
import { useConnection } from "../lib/connection-context";
import type { User } from "../lib/types";

export function AppHeader() {
  const { mode, setMode, activeConnectionId, setActiveConnection, connections } = useConnection();
  const [user, setUser] = useState<User | null>(null);
  const [showDropdown, setShowDropdown] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const activeConnection = connections.find((c) => c.connection_id === activeConnectionId);

  useEffect(() => {
    getMe().then(setUser).catch(() => {});
  }, []);

  // Close dropdown on outside click
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowDropdown(false);
      }
    }
    if (showDropdown) document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [showDropdown]);

  return (
    <header className="sticky top-0 z-50 flex items-center justify-between border-b border-border/30 bg-background/80 px-5 py-3 backdrop-blur-sm">
      {/* Left: logo + connection switcher */}
      <div className="flex items-center gap-4">
        <span
          className="text-base font-bold text-foreground"
          style={{ fontFamily: "Georgia, 'Times New Roman', serif" }}
        >
          NL Query Tool
        </span>

        {mode === "workspace" && activeConnection && (
          <div className="relative" ref={dropdownRef}>
            <button
              onClick={() => setShowDropdown(!showDropdown)}
              className="flex items-center gap-2 rounded-full border border-border/40 bg-background/60 px-3.5 py-1.5 text-sm font-medium text-foreground/80 backdrop-blur-sm transition-colors hover:border-border/60 hover:bg-background/80"
            >
              <Database className="h-3.5 w-3.5 text-primary" />
              <span className="max-w-[160px] truncate">{activeConnection.display_name}</span>
              <span className="rounded-md bg-primary/10 px-1.5 py-0.5 text-xs font-semibold text-primary">
                {activeConnection.connector_type}
              </span>
              <ChevronDown className="h-3.5 w-3.5 text-foreground/40" />
            </button>

            {showDropdown && (
              <div className="absolute left-0 top-full mt-2 min-w-[220px] overflow-hidden rounded-2xl border border-border/30 bg-background/90 shadow-xl backdrop-blur-sm">
                <div className="p-1.5">
                  {connections.map((conn) => (
                    <button
                      key={conn.connection_id}
                      onClick={() => {
                        if (!conn.query_ready) return;
                        setActiveConnection(conn.connection_id);
                        setShowDropdown(false);
                      }}
                      disabled={!conn.query_ready}
                      className={[
                        "flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-sm transition-colors",
                        conn.query_ready
                          ? "cursor-pointer hover:bg-primary/8"
                          : "cursor-not-allowed opacity-50",
                        conn.connection_id === activeConnectionId ? "bg-primary/10" : "",
                      ].join(" ")}
                    >
                      <Database className="h-3.5 w-3.5 shrink-0 text-primary/60" />
                      <div className="min-w-0 flex-1">
                        <span className="block truncate font-medium text-foreground">
                          {conn.display_name}
                        </span>
                        <span className="text-xs text-foreground/50">
                          {conn.query_ready ? conn.connector_type : "setup required"}
                        </span>
                      </div>
                      {conn.connection_id === activeConnectionId && (
                        <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
                      )}
                    </button>
                  ))}
                </div>
                <div className="border-t border-border/20 p-1.5">
                  <button
                    onClick={() => {
                      setMode("picker");
                      setShowDropdown(false);
                    }}
                    className="flex w-full items-center gap-2 rounded-xl px-3 py-2.5 text-sm font-medium text-primary transition-colors hover:bg-primary/8"
                  >
                    <Plus className="h-3.5 w-3.5" />
                    New Connection
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Right: new connection + user */}
      <div className="flex items-center gap-3">
        {mode === "workspace" && (
          <button
            onClick={() => setMode("picker")}
            className="flex items-center gap-1.5 rounded-full border border-border/40 px-3.5 py-1.5 text-sm font-medium text-foreground/70 transition-colors hover:border-border/60 hover:text-foreground"
          >
            <Plus className="h-3.5 w-3.5" />
            New Connection
          </button>
        )}
        {user?.email && (
          <span className="text-sm text-foreground/50">{user.email}</span>
        )}
      </div>
    </header>
  );
}
