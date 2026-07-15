import * as React from "react";
import { createRoot } from "react-dom/client";
import { Navigate, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";

import { AppShell } from "@/components/app-shell";
import { Skeleton } from "@/components/ui/skeleton";
import type { CategoryConfig } from "@/routes/category-page";
import "@/styles.css";

const LazyCategoryPage = React.lazy(() => import("@/routes/category-page").then((module) => ({ default: module.CategoryPage })));

const CATEGORY_CONFIG = {
  tokens: {
    title: "Token Usage",
    description: "Compare canonical processed-token volume while preserving provider buckets, model attribution, graph boundaries, and telemetry coverage.",
    modes: [
      { value: "usage", label: "Usage" },
      { value: "distribution", label: "Distribution" },
      { value: "cache-hit-rate", label: "Cache Hit Rate" },
      { value: "input-output", label: "Input vs Output" },
    ],
    highlights: [
      { label: "Processed tokens", detail: "Canonical cohort total" },
      { label: "Median per graph", detail: "Eligible session graphs only" },
      { label: "Cache hit rate", detail: "Shown with telemetry coverage" },
      { label: "Output / input", detail: "Completion and reasoning remain visible" },
    ],
    explanation: "Processed tokens use the canonical core accounting result. Prompt, cached prompt, cache write, completion, and reasoning buckets stay separate where evidence exists.",
    caveat: "More token processing is not a model-quality score. Missing provider telemetry is excluded from the relevant calculation and reported through coverage.",
    endpoint: "/api/tokens",
  },
  cost: {
    title: "Cost",
    description: "Compare supported USD evidence without repricing models or silently turning unavailable cost into zero.",
    modes: [
      { value: "per-session", label: "Cost per Session" },
      { value: "distribution", label: "Distribution" },
      { value: "total", label: "Total Cost" },
    ],
    highlights: [
      { label: "Supported cost", detail: "Reported and estimated evidence" },
      { label: "Median per graph", detail: "Priced session graphs only" },
      { label: "Reported coverage", detail: "Direct provider evidence" },
      { label: "Estimated coverage", detail: "Core-supported estimates" },
    ],
    explanation: "The plugin accepts core-emitted cost values and their evidence labels. It does not download price tables, select aliases, or multiply token totals in TypeScript.",
    caveat: "Reported, estimated, and unavailable values remain distinguishable. Unpriced graphs stay visible but do not enter numeric distributions.",
    endpoint: "/api/cost",
  },
  execution: {
    title: "Execution Time",
    description: "Compare active execution, measurable wait time, and interaction complexity without collapsing them into an ambiguous runtime score.",
    modes: [
      { value: "active", label: "Active Time" },
      { value: "distribution", label: "Distribution" },
      { value: "active-wait", label: "Active vs Wait" },
      { value: "turns", label: "Turns" },
    ],
    highlights: [
      { label: "Active execution", detail: "Sum of measurable turn durations" },
      { label: "Median active time", detail: "Eligible session graphs only" },
      { label: "Median wait time", detail: "Kept separate from active time" },
      { label: "Median turns", detail: "Workflow complexity indicator" },
    ],
    explanation: "Active execution is the sum of measurable turn execution durations. Wait time is measured between turns and stays separate from active work.",
    caveat: "Graph-level time is attributed to a model only for single-model graphs. Mixed-model graphs remain grouped as Mixed models.",
    endpoint: "/api/execution",
  },
} satisfies Record<string, CategoryConfig>;

type CategorySearch = { chart?: string; sinceDays?: number };
type ResolvedCategorySearch = Required<CategorySearch>;

function validateCategorySearch(config: CategoryConfig) {
  const validModes = new Set(config.modes.map((mode) => mode.value));
  return (search: Record<string, unknown>): CategorySearch => ({
    chart: typeof search.chart === "string" && validModes.has(search.chart) ? search.chart : undefined,
    sinceDays: Number(search.sinceDays) === 30 ? 30 : Number(search.sinceDays) === 90 ? 90 : undefined,
  });
}

const rootRoute = createRootRoute({ component: AppShell });
const indexRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: () => <Navigate to="/tokens" replace /> });

const tokensRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tokens",
  validateSearch: validateCategorySearch(CATEGORY_CONFIG.tokens),
  component: TokensRoute,
});

const costRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/cost",
  validateSearch: validateCategorySearch(CATEGORY_CONFIG.cost),
  component: CostRoute,
});

const executionRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/execution",
  validateSearch: validateCategorySearch(CATEGORY_CONFIG.execution),
  component: ExecutionRoute,
});

function resolveSearch(config: CategoryConfig, search: CategorySearch): ResolvedCategorySearch {
  return {
    chart: search.chart ?? config.modes[0].value,
    sinceDays: search.sinceDays ?? 7,
  };
}

function RoutePage({ config, search, navigate }: { config: CategoryConfig; search: ResolvedCategorySearch; navigate: (next: CategorySearch) => void }) {
  return (
    <React.Suspense fallback={<Skeleton className="min-h-64 rounded-xl border border-border" aria-label="Loading metrics route" />}>
      <LazyCategoryPage
        config={config}
        chart={search.chart}
        sinceDays={search.sinceDays}
        onChartChange={(chart) => navigate({ ...search, chart })}
        onSinceDaysChange={(sinceDays) => navigate({ ...search, sinceDays })}
      />
    </React.Suspense>
  );
}

function TokensRoute() {
  const search = resolveSearch(CATEGORY_CONFIG.tokens, tokensRoute.useSearch());
  const navigate = tokensRoute.useNavigate();
  return <RoutePage config={CATEGORY_CONFIG.tokens} search={search} navigate={(next) => void navigate({ search: next })} />;
}

function CostRoute() {
  const search = resolveSearch(CATEGORY_CONFIG.cost, costRoute.useSearch());
  const navigate = costRoute.useNavigate();
  return <RoutePage config={CATEGORY_CONFIG.cost} search={search} navigate={(next) => void navigate({ search: next })} />;
}

function ExecutionRoute() {
  const search = resolveSearch(CATEGORY_CONFIG.execution, executionRoute.useSearch());
  const navigate = executionRoute.useNavigate();
  return <RoutePage config={CATEGORY_CONFIG.execution} search={search} navigate={(next) => void navigate({ search: next })} />;
}

const router = createRouter({ routeTree: rootRoute.addChildren([indexRoute, tokensRoute, costRoute, executionRoute]) });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
