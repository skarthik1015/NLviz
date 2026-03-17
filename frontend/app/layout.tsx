"use client";

import "./globals.css";
import { ReactNode } from "react";
import { ConnectionProvider } from "./lib/connection-context";

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <title>NL Query Tool</title>
        <meta name="description" content="Natural language analytics workbench" />
      </head>
      <body>
        <ConnectionProvider>{children}</ConnectionProvider>
      </body>
    </html>
  );
}
