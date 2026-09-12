import * as React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MotionConfig } from "motion/react";
import { createRootRoute, createRoute, createRouter, lazyRouteComponent, redirect, RouterProvider } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { StateBlock } from "@/components/state-block";
import { Toaster } from "@/components/ui/sonner";
import { CommandPalette } from "@/components/command-palette";
import { SourceCapabilityGate } from "@/components/source-capability-gate";
import { DatahubDeliveryProvider } from "@/hooks/use-datahub-delivery";
import "@/styles.css";

// lazyRouteComponent (unlike React.lazy) lets the router preload the chunk on
// link hover/focus via defaultPreload: "intent".
const OverviewRoute = lazyRouteComponent(() => import("@/routes/overview"), "OverviewRoute");
const SessionsRoute = lazyRouteComponent(() => import("@/routes/sessions"), "SessionsRoute");
const SessionResolverRoute = lazyRouteComponent(() => import("@/routes/session-resolver"), "SessionResolverRoute");
const GraphOverviewRoute = lazyRouteComponent(() => import("@/routes/graph-overview"), "GraphOverviewRoute");
const SessionDetailRoute = lazyRouteComponent(() => import("@/routes/session-detail"), "SessionDetailRoute");
const ModelUsageRoute = lazyRouteComponent(() => import("@/routes/model-usage"), "ModelUsageRoute");
const CodeTimeRoute = lazyRouteComponent(() => import("@/routes/code-time"), "CodeTimeRoute");

function RouteBoundary({ children }: { children: React.ReactNode }) {
  return (
    <React.Suspense fallback={<StateBlock title="Loading view" detail="Preparing the datahub route." />}>
      {children}
    </React.Suspense>
  );
}

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 10 * 60_000,
        refetchOnWindowFocus: false,
      },
    },
  });
}

const rootRoute = createRootRoute({
  component: () => (
    <>
      <AppShell />
      <Toaster position="bottom-right" richColors />
      <CommandPaletteTrigger />
    </>
  ),
});

function CommandPaletteTrigger() {
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
  return <CommandPalette open={open} onOpenChange={setOpen} />;
}

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({
      to: "/sessions",
      search: { projectName: undefined },
      replace: true,
    });
  },
});

const sessionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions",
  validateSearch: (search: Record<string, unknown>): { projectName: string | undefined } => ({
    projectName: typeof search.projectName === "string" ? search.projectName : undefined,
  }),
  component: () => <RouteBoundary><SessionsRoute /></RouteBoundary>,
});

const todayRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/today",
  component: () => <RouteBoundary><SourceCapabilityGate capability="today"><OverviewRoute /></SourceCapabilityGate></RouteBoundary>,
});

type GraphSearch = {
  branch?: string;
};

const graphRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/graphs/$rootId",
  validateSearch: (search: Record<string, unknown>): GraphSearch => ({
    branch: typeof search.branch === "string" && search.branch ? search.branch : undefined,
  }),
  component: () => <RouteBoundary><GraphOverviewRoute /></RouteBoundary>,
});

type SessionDetailSearch = {
  tab: "context" | "timeline";
  kind?: "user" | "assistant" | "tool" | "subagent" | "compaction";
  artifact?: "file" | "command" | "check" | "commit" | "link";
  vendor?: string;
  outcome?: "failed" | "succeeded";
  entry?: string;
  turn?: string;
};

const sessionDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/graphs/$rootId/sessions/$sessionId",
  validateSearch: (search: Record<string, unknown>): SessionDetailSearch => {
    const tab = search.tab === "timeline" ? "timeline" : "context";
    const kind = search.kind === "user" || search.kind === "assistant" || search.kind === "tool" || search.kind === "subagent" || search.kind === "compaction"
      ? search.kind
      : undefined;
    const artifact = search.artifact === "file" || search.artifact === "command" || search.artifact === "check" || search.artifact === "commit" || search.artifact === "link"
      ? search.artifact
      : undefined;
    return {
      tab,
      kind: tab === "timeline" ? kind : undefined,
      artifact: tab === "timeline" ? artifact : undefined,
      vendor: tab === "timeline" && typeof search.vendor === "string" && search.vendor ? search.vendor : undefined,
      outcome: tab === "timeline" && (search.outcome === "failed" || search.outcome === "succeeded") ? search.outcome : undefined,
      entry: tab === "timeline" && typeof search.entry === "string" && search.entry ? search.entry : undefined,
      turn: tab === "timeline" && typeof search.turn === "string" && search.turn ? search.turn : undefined,
    };
  },
  component: () => <RouteBoundary><SourceCapabilityGate capability="session-detail"><SessionDetailRoute /></SourceCapabilityGate></RouteBoundary>,
});

// Resolve a session's graph identity before opening its current scoped route.
const sessionResolverRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions/$sessionId",
  validateSearch: (search: Record<string, unknown>): { tab: "context" | "timeline" } => ({
    tab: search.tab === "timeline" ? "timeline" : "context",
  }),
  component: () => <RouteBoundary><SessionResolverRoute /></RouteBoundary>,
});

type CompareSearch = {
  projectName: string | undefined;
  modelKey: string | undefined;
  view: "overview" | "cost" | "tokens" | "time" | "efficiency" | undefined;
  grain: "daily" | "weekly" | undefined;
  unit: "session" | "turn" | undefined;
};

function validateCompareSearch(search: Record<string, unknown>): CompareSearch {
  return {
    projectName: typeof search.projectName === "string" ? search.projectName : undefined,
    modelKey: typeof search.modelKey === "string" ? search.modelKey : undefined,
    view:
      search.view === "cost" ||
      search.view === "tokens" ||
      search.view === "time" ||
      search.view === "efficiency" ||
      search.view === "overview"
        ? search.view
        : undefined,
    grain:
      search.grain === "daily" || search.grain === "weekly"
        ? search.grain
        : undefined,
    unit:
      search.unit === "session" || search.unit === "turn"
        ? search.unit
        : undefined,
  };
}

const codeTimeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/code-time",
  component: () => <RouteBoundary><SourceCapabilityGate capability="code-time"><CodeTimeRoute /></SourceCapabilityGate></RouteBoundary>,
});

const compareRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/compare",
  validateSearch: validateCompareSearch,
  component: () => <RouteBoundary><SourceCapabilityGate capability="compare"><ModelUsageRoute /></SourceCapabilityGate></RouteBoundary>,
});

const router = createRouter({
  routeTree: rootRoute.addChildren([
    indexRoute,
    sessionsRoute,
    todayRoute,
    graphRoute,
    sessionDetailRoute,
    sessionResolverRoute,
    compareRoute,
    codeTimeRoute,
  ]),
  defaultPreload: "intent",
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

function DatahubApplication() {
  const [client] = React.useState(createQueryClient);
  return (
    <QueryClientProvider client={client}>
      <DatahubDeliveryProvider>
        <RouterProvider router={router} />
      </DatahubDeliveryProvider>
    </QueryClientProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MotionConfig reducedMotion="user">
      <DatahubApplication />
    </MotionConfig>
  </React.StrictMode>,
);
