"use client";

import { useEffect, useState } from "react";
import { Database } from "lucide-react";
import { getMe } from "../lib/api";
import { useConnection } from "../lib/connection-context";
import type { User } from "../lib/types";

export function AppHeader() {
  const { activeConnectionId, connections } = useConnection();
  const [user, setUser] = useState<User | null>(null);
  const activeConnection = connections.find((c) => c.connection_id === activeConnectionId);

  useEffect(() => {
    getMe().then(setUser).catch(() => {});
  }, []);

  return (
    <header className="sticky top-0 z-50 flex items-center justify-between border-b border-border/30 bg-background/80 px-5 py-3 backdrop-blur-sm">
      <div className="flex items-center gap-4">
        <span
          className="text-base font-bold text-foreground"
          style={{ fontFamily: "Georgia, 'Times New Roman', serif" }}
        >
          NL Query Tool
        </span>

        {activeConnection && (
          <div className="flex items-center gap-2 rounded-full border border-border/40 bg-background/60 px-3 py-1.5 backdrop-blur-sm">
            <Database className="h-3.5 w-3.5 text-primary" />
            <span className="max-w-[180px] truncate text-sm font-medium text-foreground/80">
              {activeConnection.display_name}
            </span>
            <span className="rounded-md bg-primary/10 px-1.5 py-0.5 text-xs font-semibold text-primary">
              {activeConnection.connector_type}
            </span>
          </div>
        )}
      </div>

      {user?.email && (
        <span className="text-sm text-foreground/50">{user.email}</span>
      )}
    </header>
  );
}
