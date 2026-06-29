import * as React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { StateBlock } from "@/components/state-block";
import { Toaster } from "@/components/ui/sonner";
import "@/styles.css";

const OverviewRoute = React.lazy(() => import("@/routes/overview").then((mod) => ({ default: mod.OverviewRoute })));
const ProjectDetailRoute = React.lazy(() => import("@/routes/projects").then((mod) => ({ default: mod.ProjectDetailRoute })));
const SessionsRoute = React.lazy(() => import("@/routes/sessions").then((mod) => ({ default: mod.SessionsRoute })));
const ContextWindowRoute = React.lazy(() => import("@/routes/context-window").then((mod) => ({ default: mod.ContextWindowRoute })));
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
      staleTime: 15_000,
      refetchOnWindowFocus: false,
    },
  },
});

const rootRoute = createRootRoute({
  component: () => (
    <>
      <AppShell />
      <Toaster position="bottom-right" richColors />
    </>
  ),
});

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
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
