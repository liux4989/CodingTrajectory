import * as React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { Toaster } from "@/components/ui/sonner";
import { OverviewRoute } from "@/routes/overview";
import { ProjectsRoute } from "@/routes/projects";
import { SessionsRoute } from "@/routes/sessions";
import { ContextWindowRoute } from "@/routes/context-window";
import { CleanupRoute } from "@/routes/cleanup";
import "@/styles.css";

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
  component: OverviewRoute,
});

const projectsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/projects",
  component: ProjectsRoute,
});

const sessionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions",
  component: SessionsRoute,
});

const contextWindowRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions/$sessionId/context-window",
  component: ContextWindowRoute,
});

const cleanupRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/cleanup",
  component: CleanupRoute,
});

const router = createRouter({
  routeTree: rootRoute.addChildren([
    indexRoute,
    projectsRoute,
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
