import * as React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
  RouterProvider,
  useRouterState,
} from "@tanstack/react-router";
import { OverviewRoute } from "@/routes/index";
import { ForecastsRoute } from "@/routes/forecasts";
import "@/styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
    },
  },
});

function NavLink({ to, label }: { to: string; label: string }) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const active = to === "/" ? pathname === "/" : pathname.startsWith(to);
  return (
    <Link
      to={to}
      className={`rounded-md px-3 py-1 text-caption font-display transition-colors ${
        active
          ? "bg-primary text-primary-foreground shadow-sm"
          : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </Link>
  );
}

function RootLayout() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-border">
        <div className="mx-auto flex max-w-6xl items-center gap-2 px-6 py-3">
          <span className="mr-3 font-display text-body-sm font-semibold tracking-tight">
            Code Time
          </span>
          <nav className="flex gap-1 rounded-lg border border-border bg-secondary/50 p-1">
            <NavLink to="/" label="Overview" />
            <NavLink to="/forecasts" label="Forecasts" />
          </nav>
        </div>
      </header>
      <Outlet />
    </div>
  );
}

const rootRoute = createRootRoute({
  component: RootLayout,
});
const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: OverviewRoute,
});
const forecastsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/forecasts",
  component: ForecastsRoute,
});

const router = createRouter({
  routeTree: rootRoute.addChildren([indexRoute, forecastsRoute]),
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
