import * as React from "react";
import { Link, useMatches } from "@tanstack/react-router";
import { ChevronRight, Home } from "lucide-react";
import { cn } from "@/lib/utils";

type Crumb = { key: string; label: string; to: string };

const LABEL_FNS: Record<string, (params: Record<string, unknown>) => string> = {
  "/": () => "Overview",
  "/model-usage": () => "Model usage",
  "/token-efficiency": () => "Token efficiency",
  "/token-efficiency/$projectName": (p) => String(p.projectName ?? "Project"),
  "/token-efficiency/$projectName/patterns": () => "Patterns",
  "/token-efficiency/$projectName/patterns/$patternKey": (p) =>
    humanize(String(p.patternKey ?? "Pattern")),
  "/token-efficiency/$projectName/hotspots": () => "Hotspots",
  "/token-efficiency/$projectName/hotspots/$hotspotKey": () => "Hotspot",
  "/token-efficiency/$projectName/outliers": () => "Outliers",
  "/sessions": () => "Sessions",
  "/sessions/$sessionId/context-window": (p) => {
    const id = String(p.sessionId ?? "");
    return id.length > 12 ? id.slice(0, 12) : id || "Session";
  },
};

type Props = {
  className?: string;
};

export function Breadcrumbs({ className }: Props) {
  const matches = useMatches();
  const leaf = matches.at(-1);
  const crumbs: Crumb[] =
    leaf?.pathname.startsWith("/token-efficiency")
      ? tokenEfficiencyCrumbs(
          leaf.pathname,
          leaf.params as Record<string, unknown>,
        )
      : matches
          .filter((m) => m.routeId !== "__root__" && m.pathname !== "/")
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
        to="/"
        preload="intent"
        className="inline-flex items-center gap-1 rounded transition-colors hover:text-foreground"
      >
        <Home size={14} />
        <span>Overview</span>
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

function tokenEfficiencyCrumbs(
  pathname: string,
  params: Record<string, unknown>,
): Crumb[] {
  const crumbs: Crumb[] = [
    {
      key: "token-efficiency",
      label: "Token efficiency",
      to: "/token-efficiency",
    },
  ];
  const projectName = String(params.projectName ?? "");
  if (!projectName) return crumbs;
  const projectPath = `/token-efficiency/${encodeURIComponent(projectName)}`;
  crumbs.push({
    key: "token-efficiency-project",
    label: projectName,
    to: projectPath,
  });
  if (pathname.includes("/patterns")) {
    const patternsPath = `${projectPath}/patterns`;
    crumbs.push({
      key: "token-efficiency-patterns",
      label: "Patterns",
      to: patternsPath,
    });
    const patternKey = String(params.patternKey ?? "");
    if (patternKey) {
      crumbs.push({
        key: "token-efficiency-pattern",
        label: humanize(patternKey),
        to: pathname,
      });
    }
  } else if (pathname.includes("/hotspots")) {
    const hotspotsPath = `${projectPath}/hotspots`;
    crumbs.push({
      key: "token-efficiency-hotspots",
      label: "Hotspots",
      to: hotspotsPath,
    });
    if (params.hotspotKey) {
      crumbs.push({
        key: "token-efficiency-hotspot",
        label: "Hotspot",
        to: pathname,
      });
    }
  } else if (pathname.endsWith("/outliers")) {
    crumbs.push({
      key: "token-efficiency-outliers",
      label: "Outliers",
      to: pathname,
    });
  }
  return crumbs;
}

function humanize(routeId: string) {
  const tail = routeId.replace(/^\//, "").split("/").pop() ?? routeId;
  return tail
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
