import { ChatWorkbench } from "./components/chat-workbench";

export default function HomePage() {
  return (
    <main className="shell">
      <section className="hero">
        <div className="eyebrow">NL Query Tool</div>
        <h1 className="title">Ask the dataset. Inspect the pipeline.</h1>
        <p className="subtitle">
          This minimal frontend exercises the current backend slice: natural-language question in, semantic
          intent out, deterministic SQL compiled, tabular result returned, and a lightweight plot spec derived
          from the response.
        </p>
      </section>
      <ChatWorkbench />
    </main>
  );
}
