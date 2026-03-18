"use client";

import { useConnection } from "./lib/connection-context";
import { AppHeader } from "./components/app-header";
import { ChatWorkbench } from "./components/chat-workbench";
import { ConnectionPicker } from "./components/connection-picker";

export default function HomePage() {
  const { mode, loading } = useConnection();

  if (loading) {
    return (
      <main className="shell">
        <div style={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "60vh" }}>
          <p style={{ color: "var(--muted)", fontSize: "1.1rem" }}>Loading...</p>
        </div>
      </main>
    );
  }

  if (mode === "picker") {
    return <ConnectionPicker />;
  }

  return (
    <main className="shell">
      <AppHeader />
      <section className="hero">
        <div className="eyebrow">NL Query Tool</div>
        <h1 className="title">Ask your data anything.</h1>
        <p className="subtitle">
          Type a natural-language question and get SQL, tabular results, and charts — powered by your connected database.
        </p>
      </section>
      <ChatWorkbench />
    </main>
  );
}
