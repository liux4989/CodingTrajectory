import { Link, Outlet, useMatchRoute } from "@tanstack/react-router";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "@/hooks/use-theme";
import { Button } from "@/components/ui/button";
import { DateRangeToggle } from "@/components/date-range-toggle";
import { NavDropdown, type NavDropdownItem } from "@/components/nav-dropdown";
import { Breadcrumbs } from "@/components/breadcrumbs";
import { cn } from "@/lib/utils";

const ANALYTICS_ITEMS: NavDropdownItem[] = [
  {
    to: "/model-usage",
    label: "Model usage",
    description: "Cost, tokens, and time per model.",
  },
  {
    to: "/error-collection",
    label: "Errors",
    description: "Session-level coding failures and warnings.",
  },
];

function navLinkClass(active: boolean) {
  return cn(
    "rounded-md px-3 py-1.5 font-medium transition-colors",
    active ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground",
  );
}

export function AppShell() {
  const { theme, toggle } = useTheme();
  const matchRoute = useMatchRoute();
  const analyticsActive =
    matchRoute({ to: "/model-usage" }) || matchRoute({ to: "/error-collection" });

  return (
    <main className="grid min-h-dvh grid-cols-1">
      <header className="sticky top-0 z-[90] flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-b border-sidebar-border bg-[linear-gradient(135deg,rgb(255_249_234/94%),rgb(215_200_164/34%)),var(--paper-strong)] px-[clamp(1rem,2vw,2rem)] py-3 backdrop-blur-[18px] dark:border-border-subtle dark:bg-[linear-gradient(135deg,rgb(34_32_25/94%),rgb(58_54_44/34%)),var(--paper-strong)]">
        <div className="flex items-center gap-3">
          <div className="grid h-[2.5rem] w-[2.5rem] place-items-center rounded-[0.9rem] border border-foreground/18 bg-foreground text-background font-display text-xs font-extrabold tracking-wide shadow-lg">
            CT
          </div>
          <div className="leading-tight">
            <p className="m-0 font-display text-eyebrow font-extrabold uppercase tracking-wider text-primary">
              Plugin Web Program
            </p>
            <h1 className="m-0 font-display text-[1rem] font-bold tracking-tight">CodingTrajectory</h1>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <DateRangeToggle />
          <nav className="flex items-center gap-1 rounded-lg border border-border-subtle bg-background/50 p-1 text-body-sm">
            <Link
              to="/"
              preload="intent"
              className={navLinkClass(Boolean(matchRoute({ to: "/" })))}
            >
              Overview
            </Link>
            <NavDropdown label="Analytics" items={ANALYTICS_ITEMS} active={Boolean(analyticsActive)} />
            <Link
              to="/sessions"
              preload="intent"
              className={navLinkClass(Boolean(matchRoute({ to: "/sessions", fuzzy: true })))}
            >
              Sessions
            </Link>
            <Link
              to="/cleanup"
              preload="intent"
              className={navLinkClass(Boolean(matchRoute({ to: "/cleanup" })))}
            >
              Cleanup
            </Link>
          </nav>
          <Button
            variant="outline"
            size="icon"
            onClick={toggle}
            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          >
            {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
          </Button>
        </div>
      </header>
      <section className="min-w-0 p-[clamp(1rem,2vw,2rem)]">
        <Breadcrumbs className="mb-4" />
        <Outlet />
      </section>
    </main>
  );
}
