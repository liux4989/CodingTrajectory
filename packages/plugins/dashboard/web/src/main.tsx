import * as React from "react";
import { createRoot } from "react-dom/client";
import { MotionConfig } from "motion/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { StateBlock } from "@/components/state-block";
import { Toaster } from "@/components/ui/sonner";
import { DateRangeProvider } from "@/hooks/use-date-range";
import { CommandPalette } from "@/components/command-palette";
import "@/styles.css";

const OverviewRoute = React.lazy(() => import("@/routes/overview").then((mod) => ({ default: mod.OverviewRoute })));
const ProjectDetailRoute = React.lazy(() => import("@/routes/projects").then((mod) => ({ default: mod.ProjectDetailRoute })));
const SessionsRoute = React.lazy(() => import("@/routes/sessions").then((mod) => ({ default: mod.SessionsRoute })));
const ContextWindowRoute = React.lazy(() => import("@/routes/context-window").then((mod) => ({ default: mod.ContextWindowRoute })));
const ModelUsageRoute = React.lazy(() => import("@/routes/model-usage").then((mod) => ({ default: mod.ModelUsageRoute })));
const TokenEfficiencyIndexRoute = React.lazy(() => import("@/routes/token-efficiency").then((mod) => ({ default: mod.TokenEfficiencyIndexRoute })));
const TokenEfficiencyProjectRoute = React.lazy(() => import("@/routes/token-efficiency").then((mod) => ({ default: mod.TokenEfficiencyProjectRoute })));
const TokenEfficiencyPatternsRoute = React.lazy(() => import("@/routes/token-efficiency").then((mod) => ({ default: mod.TokenEfficiencyPatternsRoute })));
const TokenEfficiencyPatternDetailRoute = React.lazy(() => import("@/routes/token-efficiency").then((mod) => ({ default: mod.TokenEfficiencyPatternDetailRoute })));
const TokenEfficiencyHotspotsRoute = React.lazy(() => import("@/routes/token-efficiency").then((mod) => ({ default: mod.TokenEfficiencyHotspotsRoute })));
const TokenEfficiencyHotspotDetailRoute = React.lazy(() => import("@/routes/token-efficiency").then((mod) => ({ default: mod.TokenEfficiencyHotspotDetailRoute })));
const TokenEfficiencyOutliersRoute = React.lazy(() => import("@/routes/token-efficiency").then((mod) => ({ default: mod.TokenEfficiencyOutliersRoute })));
const CacheBreaksRoute = React.lazy(() => import("@/routes/cache-breaks").then((mod) => ({ default: mod.CacheBreaksRoute })));
const ErrorCollectionRoute = React.lazy(() => import("@/routes/error-collection").then((mod) => ({ default: mod.ErrorCollectionRoute })));
const CleanupRoute = React.lazy(() => import("@/routes/cleanup").then((mod) => ({ default: mod.CleanupRoute })));

function RouteBoundary({ children }: { children: React.ReactNode }) {
  return (
    <React.Suspense fallback={<StateBlock title="Loading view" detail="Preparing the dashboard route." />}>
      {children}
    </React.Suspense>
  );
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
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
  component: () => <RouteBoundary><OverviewRoute /></RouteBoundary>,
});

const projectDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/projects/$projectName",
  validateSearch: (search: Record<string, unknown>): { sinceDays: number | undefined } => ({
    sinceDays:
      search.sinceDays != null && !Number.isNaN(Number(search.sinceDays))
        ? Number(search.sinceDays)
        : undefined,
  }),
  component: () => <RouteBoundary><ProjectDetailRoute /></RouteBoundary>,
});

const sessionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions",
  component: () => <RouteBoundary><SessionsRoute /></RouteBoundary>,
});

const contextWindowRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions/$sessionId/context-window",
  component: () => <RouteBoundary><ContextWindowRoute /></RouteBoundary>,
});

const modelUsageRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/model-usage",
  validateSearch: (search: Record<string, unknown>): {
    projectName: string | undefined;
    modelKey: string | undefined;
    view: "overview" | "cost" | "tokens" | "time" | undefined;
  } => ({
    projectName: typeof search.projectName === "string" ? search.projectName : undefined,
    modelKey: typeof search.modelKey === "string" ? search.modelKey : undefined,
    view:
      search.view === "cost" || search.view === "tokens" || search.view === "time" || search.view === "overview"
        ? search.view
        : undefined,
  }),
  component: () => <RouteBoundary><ModelUsageRoute /></RouteBoundary>,
});

type TokenEfficiencySearch = {
  grain: "daily" | "weekly" | undefined;
  unit: "session" | "turn" | undefined;
};

function validateTokenEfficiencySearch(
  search: Record<string, unknown>,
): TokenEfficiencySearch {
  return {
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

const tokenEfficiencyIndexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/token-efficiency",
  component: () => <RouteBoundary><TokenEfficiencyIndexRoute /></RouteBoundary>,
});

const tokenEfficiencyProjectRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/token-efficiency/$projectName",
  validateSearch: validateTokenEfficiencySearch,
  component: () => <RouteBoundary><TokenEfficiencyProjectRoute /></RouteBoundary>,
});

const tokenEfficiencyPatternsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/token-efficiency/$projectName/patterns",
  validateSearch: validateTokenEfficiencySearch,
  component: () => <RouteBoundary><TokenEfficiencyPatternsRoute /></RouteBoundary>,
});

const tokenEfficiencyPatternDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/token-efficiency/$projectName/patterns/$patternKey",
  validateSearch: validateTokenEfficiencySearch,
  component: () => <RouteBoundary><TokenEfficiencyPatternDetailRoute /></RouteBoundary>,
});

const tokenEfficiencyHotspotsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/token-efficiency/$projectName/hotspots",
  validateSearch: validateTokenEfficiencySearch,
  component: () => <RouteBoundary><TokenEfficiencyHotspotsRoute /></RouteBoundary>,
});

const tokenEfficiencyHotspotDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/token-efficiency/$projectName/hotspots/$hotspotKey",
  validateSearch: validateTokenEfficiencySearch,
  component: () => <RouteBoundary><TokenEfficiencyHotspotDetailRoute /></RouteBoundary>,
});

const tokenEfficiencyOutliersRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/token-efficiency/$projectName/outliers",
  validateSearch: validateTokenEfficiencySearch,
  component: () => <RouteBoundary><TokenEfficiencyOutliersRoute /></RouteBoundary>,
});

const cacheBreaksRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/cache-breaks",
  validateSearch: (search: Record<string, unknown>): { projectName: string | undefined } => ({
    projectName: typeof search.projectName === "string" ? search.projectName : undefined,
  }),
  component: () => <RouteBoundary><CacheBreaksRoute /></RouteBoundary>,
});

const errorCollectionRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/error-collection",
  validateSearch: (search: Record<string, unknown>): { projectName: string | undefined } => ({
    projectName: typeof search.projectName === "string" ? search.projectName : undefined,
  }),
  component: () => <RouteBoundary><ErrorCollectionRoute /></RouteBoundary>,
});

const cleanupRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/cleanup",
  component: () => <RouteBoundary><CleanupRoute /></RouteBoundary>,
});

const router = createRouter({
  routeTree: rootRoute.addChildren([
    indexRoute,
    projectDetailRoute,
    sessionsRoute,
    contextWindowRoute,
    modelUsageRoute,
    tokenEfficiencyIndexRoute,
    tokenEfficiencyProjectRoute,
    tokenEfficiencyPatternsRoute,
    tokenEfficiencyPatternDetailRoute,
    tokenEfficiencyHotspotsRoute,
    tokenEfficiencyHotspotDetailRoute,
    tokenEfficiencyOutliersRoute,
    cacheBreaksRoute,
    errorCollectionRoute,
    cleanupRoute,
  ]),
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MotionConfig reducedMotion="user">
      <QueryClientProvider client={queryClient}>
        <DateRangeProvider>
          <RouterProvider router={router} />
        </DateRangeProvider>
      </QueryClientProvider>
    </MotionConfig>
  </React.StrictMode>,
);
