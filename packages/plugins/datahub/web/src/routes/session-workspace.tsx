import { useSearch } from "@tanstack/react-router";
import { ContextWindowRoute } from "@/routes/context-window";
import { SessionGraphRoute } from "@/routes/session-graph";
import { SessionTreeRoute } from "@/routes/session-tree";

/** Selects one existing session experience from the canonical session route. */
export function SessionWorkspaceRoute() {
  const { view } = useSearch({ from: "/sessions/$sessionId" });

  if (view === "tree") return <SessionTreeRoute />;
  if (view === "graph") return <SessionGraphRoute />;
  return <ContextWindowRoute />;
}
