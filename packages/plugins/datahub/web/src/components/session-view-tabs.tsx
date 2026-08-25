import * as React from "react";
import { Link } from "@tanstack/react-router";
import { cn } from "@/lib/utils";

type SessionViewTabsProps = {
  sessionId: string;
  active: "context" | "tree" | "graph";
};

const tabs = [
  { id: "context", label: "Context window", to: "/sessions/$sessionId" },
  { id: "tree", label: "Conversation tree", to: "/sessions/$sessionId/tree" },
  { id: "graph", label: "Agent graph", to: "/sessions/$sessionId/graph" },
] as const;

/** Route-level switcher between the per-session context window and graph views. */
export function SessionViewTabs({ sessionId, active }: SessionViewTabsProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border-soft pb-2">
      <nav className="flex flex-wrap gap-2" aria-label="Session views">
        {tabs.map((tab) => {
          const isActive = tab.id === active;
          return (
            <Link
              key={tab.id}
              to={tab.to}
              params={{ sessionId }}
              role="tab"
              aria-selected={isActive}
              className={cn(
                "inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-body-sm font-medium transition-colors",
                isActive
                  ? "bg-primary text-primary-foreground shadow-sm"
                  : "text-muted-foreground hover:bg-surface-emphasis hover:text-foreground",
              )}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
