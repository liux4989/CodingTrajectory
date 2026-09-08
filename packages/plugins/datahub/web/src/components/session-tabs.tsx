import * as React from "react";
import { Link } from "@tanstack/react-router";
import { cn } from "@/lib/utils";

type SessionTabsProps = {
  rootId: string;
  sessionId: string;
  active: "context" | "timeline";
};

const tabs = [
  { id: "context", label: "Context window" },
  { id: "timeline", label: "Timeline" },
] as const;

/** Session-scope switcher: context pressure vs recorded evidence for one session. */
export function SessionTabs({ rootId, sessionId, active }: SessionTabsProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border-soft pb-2">
      <nav className="flex flex-wrap gap-2" aria-label="Session views">
        {tabs.map((tab) => {
          const isActive = tab.id === active;
          return (
            <Link
              key={tab.id}
              to="/graphs/$rootId/sessions/$sessionId"
              params={{ rootId, sessionId }}
              search={{ tab: tab.id }}
              aria-current={isActive ? "page" : undefined}
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
