import * as React from "react";
import { Link } from "@tanstack/react-router";
import { sectionTabClass } from "@/components/section-tabs";

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
              className={sectionTabClass(isActive)}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
