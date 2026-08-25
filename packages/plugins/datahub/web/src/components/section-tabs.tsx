import * as React from "react";
import { motion } from "motion/react";
import { fadeSoft } from "@/lib/motion";
import { cn } from "@/lib/utils";

type SectionTab = {
  id: string;
  label: string;
  /** Optional badge count shown next to the label. */
  badge?: number | string;
  content: React.ReactNode;
};

type SectionTabsProps = {
  tabs: SectionTab[];
  /** Controlled active tab ID. */
  activeTab: string;
  onTabChange: (id: string) => void;
  /** Optional content rendered above the tab bar (e.g. summary cards). */
  summary?: React.ReactNode;
  /** Accessible label for the tablist. */
  ariaLabel?: string;
  className?: string;
};

/**
 * Section tabs with an always-visible summary slot above the tab bar. The
 * summary renders once and stays in place as users switch tabs, so key
 * metrics remain visible regardless of which detail tab is active.
 *
 * Designed to be controlled by the parent route (URL search params or local
 * state), following the same pattern as model-usage's view toggle.
 */
export function SectionTabs({
  tabs,
  activeTab,
  onTabChange,
  summary,
  ariaLabel = "Section tabs",
  className,
}: SectionTabsProps) {
  const active = tabs.find((t) => t.id === activeTab) ?? tabs[0];

  return (
    <div className={cn("grid gap-4", className)}>
      {summary}
      <div className="flex flex-wrap items-center gap-2 border-b border-border-soft pb-2">
        <nav className="flex flex-wrap gap-2" aria-label={ariaLabel}>
          {tabs.map((tab) => {
            const isActive = tab.id === active?.id;
            return (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={isActive}
                onClick={() => onTabChange(tab.id)}
                className={cn(
                  "inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-body-sm font-medium transition-colors",
                  isActive
                    ? "bg-primary text-primary-foreground shadow-sm"
                    : "text-muted-foreground hover:bg-surface-emphasis hover:text-foreground",
                )}
              >
                {tab.label}
                {tab.badge != null ? (
                  <span
                    className={cn(
                      "rounded-full px-1.5 py-0 text-caption font-bold",
                      isActive
                        ? "bg-primary-foreground/20 text-primary-foreground"
                        : "bg-surface-emphasis text-muted-foreground",
                    )}
                  >
                    {tab.badge}
                  </span>
                ) : null}
              </button>
            );
          })}
        </nav>
      </div>
      <motion.div
        key={active?.id}
        variants={fadeSoft}
        initial="hidden"
        animate="visible"
      >
        {active?.content}
      </motion.div>
    </div>
  );
}
