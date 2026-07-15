import { Link, Outlet } from "@tanstack/react-router";
import { Activity, Coins, Gauge, Timer } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/tokens", label: "Token Usage", icon: Activity },
  { to: "/cost", label: "Cost", icon: Coins },
  { to: "/execution", label: "Execution Time", icon: Timer },
] as const;

export function AppShell() {
  return (
    <div className="min-h-dvh">
      <header className="border-b border-border/80 bg-background/90 backdrop-blur-sm">
        <div className="mx-auto flex max-w-[96rem] flex-wrap items-center gap-4 px-4 py-4 sm:px-6 lg:px-8">
          <Link to="/tokens" className="flex min-w-0 items-center gap-3 text-foreground no-underline">
            <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-primary text-primary-foreground shadow-sm">
              <Gauge aria-hidden="true" />
            </span>
            <span className="min-w-0">
              <span className="block truncate font-display text-lg font-bold">CodingTrajectory Metrics</span>
              <span className="block truncate text-xs text-muted-foreground">Canonical session comparisons</span>
            </span>
          </Link>
          <Badge variant="outline" className="hidden sm:inline-flex">Phase 1 shell</Badge>
          <nav className="flex w-full flex-wrap gap-1 sm:ml-auto sm:w-auto" aria-label="Metric categories">
            {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
              <Link
                key={to}
                to={to}
                className={cn("inline-flex min-h-9 items-center gap-2 rounded-lg px-3 text-sm font-medium text-muted-foreground no-underline transition-colors hover:bg-accent hover:text-accent-foreground")}
                activeProps={{ className: "bg-accent text-accent-foreground" }}
              >
                <Icon aria-hidden="true" />
                {label}
              </Link>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto grid max-w-[96rem] gap-6 px-4 py-6 sm:px-6 lg:px-8 lg:py-10">
        <Outlet />
      </main>
      <footer className="mx-auto max-w-[96rem] px-4 pb-8 sm:px-6 lg:px-8">
        <Separator />
        <p className="mt-4 text-xs text-muted-foreground">Metrics remain unavailable until supported by canonical ct service evidence.</p>
      </footer>
    </div>
  );
}
