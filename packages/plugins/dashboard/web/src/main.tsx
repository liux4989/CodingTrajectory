import * as React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { StateBlock } from "@/components/state-block";
import { Toaster } from "@/components/ui/sonner";
import { DateRangeProvider } from "@/hooks/use-date-range";
import { CommandPalette } from "@/components/command-palette";
import { DashboardDeliveryProvider } from "@/hooks/use-dashboard-delivery";
import "@/styles.css";

const OverviewRoute = React.lazy(() => import("@/routes/overview").then((mod) => ({ default: mod.OverviewRoute })));
const SessionsRoute = React.lazy(() => import("@/routes/sessions").then((mod) => ({ default: mod.SessionsRoute })));
const ContextWindowRoute = React.lazy(() => import("@/routes/context-window").then((mod) => ({ default: mod.ContextWindowRoute })));
const ModelUsageRoute = React.lazy(() => import("@/routes/model-usage").then((mod) => ({ default: mod.ModelUsageRoute })));

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
  component: () => <RouteBoundary><OverviewRoute /></RouteBoundary>,
});

const sessionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions",
  validateSearch: (search: Record<string, unknown>): { projectName: string | undefined } => ({
    projectName: typeof search.projectName === "string" ? search.projectName : undefined,
  }),
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
    view: "overview" | "cost" | "tokens" | "time" | "efficiency" | undefined;
    grain: "daily" | "weekly" | undefined;
    unit: "session" | "turn" | undefined;
  } => ({
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
  }),
  component: () => <RouteBoundary><ModelUsageRoute /></RouteBoundary>,
});

const router = createRouter({
  routeTree: rootRoute.addChildren([
    indexRoute,
    sessionsRoute,
    contextWindowRoute,
    modelUsageRoute,
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
      <DashboardDeliveryProvider>
        <DateRangeProvider>
          <RouterProvider router={router} />
        </DateRangeProvider>
      </DashboardDeliveryProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
