"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { listConnections } from "./api";
import type { ConnectionProfile } from "./types";

type AppMode = "picker" | "workspace";

type ConnectionContextValue = {
  /** Current UI mode — picker (choose/create connection) or workspace (chat) */
  mode: AppMode;
  setMode: (mode: AppMode) => void;
  /** Active connection ID sent via X-Connection-Id header */
  activeConnectionId: string | null;
  setActiveConnection: (id: string) => void;
  /** All connections belonging to the current user */
  connections: ConnectionProfile[];
  /** Re-fetch connections from the API */
  refreshConnections: () => Promise<void>;
  /** True while the initial connection list is loading */
  loading: boolean;
};

const ConnectionContext = createContext<ConnectionContextValue | null>(null);

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<AppMode>("picker");
  const [activeConnectionId, setActiveConnectionId] = useState<string | null>(null);
  const [connections, setConnections] = useState<ConnectionProfile[]>([]);
  const [loading, setLoading] = useState(true);

  const reconcileConnections = useCallback(
    (list: ConnectionProfile[]) => {
      setConnections(list);
      const stillValid = activeConnectionId
        ? list.find((conn) => conn.connection_id === activeConnectionId)
        : null;
      if (stillValid) {
        setActiveConnectionId(stillValid.connection_id);
        setMode("workspace");
        return;
      }
      const firstReady = list.find((conn) => conn.query_ready);
      if (firstReady) {
        setActiveConnectionId(firstReady.connection_id);
        setMode("workspace");
        return;
      }
      setActiveConnectionId(null);
      setMode("picker");
    },
    [activeConnectionId, setMode],
  );

  const refreshConnections = useCallback(async () => {
    try {
      const list = await listConnections();
      reconcileConnections(list);
    } catch {
      // If auth fails or API is down, show the picker anyway
      setConnections([]);
      setActiveConnectionId(null);
      setMode("picker");
    }
  }, [reconcileConnections, setMode]);

  // On mount: load connections and decide initial mode
  useEffect(() => {
    (async () => {
      try {
        const list = await listConnections();
        setConnections(list);
        const firstReady = list.find((conn) => conn.query_ready);
        if (firstReady) {
          setActiveConnectionId(firstReady.connection_id);
          setMode("workspace");
        } else {
          setMode("picker");
        }
      } catch {
        setConnections([]);
        setMode("picker");
      } finally {
        setLoading(false);
      }
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const setActiveConnection = useCallback((id: string) => {
    setActiveConnectionId(id);
    setMode("workspace");
  }, []);

  return (
    <ConnectionContext.Provider
      value={{
        mode,
        setMode,
        activeConnectionId,
        setActiveConnection,
        connections,
        refreshConnections,
        loading,
      }}
    >
      {children}
    </ConnectionContext.Provider>
  );
}

export function useConnection() {
  const ctx = useContext(ConnectionContext);
  if (!ctx) throw new Error("useConnection must be used within ConnectionProvider");
  return ctx;
}
