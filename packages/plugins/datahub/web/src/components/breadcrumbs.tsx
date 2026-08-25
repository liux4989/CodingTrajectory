import * as React from "react";
import { Link, useMatches } from "@tanstack/react-router";
import { ChevronRight, Home } from "lucide-react";
import { cn } from "@/lib/utils";

type Crumb = { key: string; label: string; to: string };

const LABEL_FNS: Record<string, (params: Record<string, unknown>) => string> = {
  "/today": () => "Today",
  "/compare": () => "Compare",
  "/sessions": () => "Sessions",
  "/sessions/$sessionId": (p) => {
    const id = String(p.sessionId ?? "");
    return id.length > 12 ? id.slice(0, 12) : id || "Session";
  },
  "/sessions/$sessionId/tree": () => "Conversation tree",
  "/sessions/$sessionId/graph": () => "Agent graph",
};

type Props = {
  className?: string;
};

export function Breadcrumbs({ className }: Props) {
  const matches = useMatches();
  const crumbs: Crumb[] = matches
    .filter((m) => m.routeId !== "__root__" && m.pathname !== "/" && m.routeId !== "/sessions")
    .map((m) => {
      const fn = LABEL_FNS[m.routeId];
      const label = fn
        ? fn(m.params as Record<string, unknown>)
        : humanize(m.routeId);
      return { key: m.id, label, to: m.pathname };
    });

  if (crumbs.length === 0) return null;

  return (
    <nav
      aria-label="Breadcrumb"
      className={cn("flex flex-wrap items-center gap-1.5 text-body-sm text-muted-foreground", className)}
    >
      <Link
        to="/sessions"
        search={{ projectName: undefined }}
        preload="intent"
        className="inline-flex items-center gap-1 rounded transition-colors hover:text-foreground"
      >
        <Home size={14} />
        <span>Sessions</span>
      </Link>
      {crumbs.map((crumb, index) => {
        const isLeaf = index === crumbs.length - 1;
        return (
          <React.Fragment key={crumb.key}>
            <ChevronRight size={14} className="opacity-50" aria-hidden="true" />
            {isLeaf ? (
              <span className="font-medium text-foreground" aria-current="page">
                {crumb.label}
              </span>
            ) : (
              <Link
                to={crumb.to}
                preload="intent"
                className="rounded transition-colors hover:text-foreground"
              >
                {crumb.label}
              </Link>
            )}
          </React.Fragment>
        );
      })}
    </nav>
  );
}

function humanize(routeId: string) {
  const tail = routeId.replace(/^\//, "").split("/").pop() ?? routeId;
  return tail
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
