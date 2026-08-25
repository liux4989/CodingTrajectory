import * as React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRootRoute, createRoute, createRouter, redirect, RouterProvider } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { StateBlock } from "@/components/state-block";
import { Toaster } from "@/components/ui/sonner";
import { CommandPalette } from "@/components/command-palette";
import { DatahubDeliveryProvider } from "@/hooks/use-datahub-delivery";
import "@/styles.css";

const OverviewRoute = React.lazy(() => import("@/routes/overview").then((mod) => ({ default: mod.OverviewRoute })));
const SessionsRoute = React.lazy(() => import("@/routes/sessions").then((mod) => ({ default: mod.SessionsRoute })));
const SessionWorkspaceRoute = React.lazy(() => import("@/routes/session-workspace").then((mod) => ({ default: mod.SessionWorkspaceRoute })));
const ModelUsageRoute = React.lazy(() => import("@/routes/model-usage").then((mod) => ({ default: mod.ModelUsageRoute })));

function RouteBoundary({ children }: { children: React.ReactNode }) {
  return (
    <React.Suspense fallback={<StateBlock title="Loading view" detail="Preparing the datahub route." />}>
      {children}
    </React.Suspense>
  );
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      gcTime: 10 * 60_000,
      refetchOnWindowFocus: false,
    },
  },
});

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
  component: () => <RouteBoundary><OverviewRoute /></RouteBoundary>,
});

type SessionWorkspaceSearch = {
  view: "timeline" | "context" | "tree" | "graph";
  kind?: "user" | "assistant" | "tool" | "subagent" | "compaction";
  agent?: string;
  outcome?: "failed" | "succeeded";
  entry?: string;
};

const contextWindowRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions/$sessionId",
  validateSearch: (search: Record<string, unknown>): SessionWorkspaceSearch => {
    const view = search.view === "timeline" || search.view === "tree" || search.view === "graph"
      ? search.view
      : "context";
    const kind = search.kind === "user" || search.kind === "assistant" || search.kind === "tool" || search.kind === "subagent" || search.kind === "compaction"
      ? search.kind
      : undefined;
    return {
      view,
      kind: view === "timeline" ? kind : undefined,
      agent: view === "timeline" && typeof search.agent === "string" && search.agent ? search.agent : undefined,
      outcome: view === "timeline" && (search.outcome === "failed" || search.outcome === "succeeded") ? search.outcome : undefined,
      entry: view === "timeline" && typeof search.entry === "string" && search.entry ? search.entry : undefined,
    };
  },
  component: () => <RouteBoundary><SessionWorkspaceRoute /></RouteBoundary>,
});

const sessionGraphRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions/$sessionId/graph",
  beforeLoad: ({ params }) => {
    throw redirect({
      to: "/sessions/$sessionId",
      params: { sessionId: params.sessionId },
      search: { view: "graph" },
      replace: true,
    });
  },
});

const sessionTimelineRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions/$sessionId/timeline",
  beforeLoad: ({ params }) => {
    throw redirect({
      to: "/sessions/$sessionId",
      params: { sessionId: params.sessionId },
      search: { view: "timeline" },
      replace: true,
    });
  },
});

const sessionTreeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions/$sessionId/tree",
  beforeLoad: ({ params }) => {
    throw redirect({
      to: "/sessions/$sessionId",
      params: { sessionId: params.sessionId },
      search: { view: "tree" },
      replace: true,
    });
  },
});

const legacyContextWindowRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions/$sessionId/context-window",
  beforeLoad: ({ params }) => {
    throw redirect({
      to: "/sessions/$sessionId",
      params: { sessionId: params.sessionId },
      search: { view: "context" },
      replace: true,
    });
  },
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

const compareRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/compare",
  validateSearch: validateCompareSearch,
  component: () => <RouteBoundary><ModelUsageRoute /></RouteBoundary>,
});

const legacyModelUsageRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/model-usage",
  validateSearch: validateCompareSearch,
  beforeLoad: ({ search }) => {
    throw redirect({
      to: "/compare",
      search,
      replace: true,
    });
  },
});

const router = createRouter({
  routeTree: rootRoute.addChildren([
    indexRoute,
    sessionsRoute,
    todayRoute,
    contextWindowRoute,
    sessionGraphRoute,
    sessionTimelineRoute,
    sessionTreeRoute,
    legacyContextWindowRoute,
    compareRoute,
    legacyModelUsageRoute,
  ]),
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <DatahubDeliveryProvider>
        <RouterProvider router={router} />
      </DatahubDeliveryProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
