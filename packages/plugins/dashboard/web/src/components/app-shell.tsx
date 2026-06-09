import * as React from "react";
import { Link, Outlet } from "@tanstack/react-router";
import { Activity, FolderGit2, Moon, Sparkles, Sun, Menu, X } from "lucide-react";
import { useTheme } from "@/hooks/use-theme";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export function AppShell() {
  const { theme, toggle } = useTheme();
  const [sidebarOpen, setSidebarOpen] = React.useState(false);

  return (
    <main className="grid min-h-dvh grid-cols-1 max-lg:grid-cols-1 lg:grid-cols-[minmax(16rem,19rem)_minmax(0,1fr)]">
      <Button
        variant="outline"
        size="icon"
        className="fixed top-4 left-4 z-[100] lg:hidden"
        onClick={() => setSidebarOpen((open) => !open)}
        aria-label={sidebarOpen ? "Close navigation" : "Open navigation"}
      >
        {sidebarOpen ? <X size={22} /> : <Menu size={22} />}
      </Button>
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} theme={theme} onToggleTheme={toggle} />
      <section className="min-w-0 p-[clamp(1rem,2vw,2rem)] max-lg:pt-16">
        <Outlet />
      </section>
    </main>
  );
}

type SidebarProps = {
  open: boolean;
  onClose: () => void;
  theme: string;
  onToggleTheme: () => void;
};

const navLinkClass = cn(
  "flex min-h-[2.9rem] items-center gap-3 rounded-full border border-transparent px-3.5 font-display font-semibold text-foreground no-underline",
  "hover:border-primary/24 hover:bg-primary/11 hover:text-primary",
  "[&.is-active]:border-primary/24 [&.is-active]:bg-primary/11 [&.is-active]:text-primary",
);

function Sidebar({ open, onClose, theme, onToggleTheme }: SidebarProps) {
  return (
    <>
      {open ? (
        <div className="fixed inset-0 z-[80] bg-black/30 lg:hidden" onClick={onClose} />
      ) : null}
      <aside
        className={cn(
          "flex flex-col gap-8 border-r border-sidebar-border p-5 backdrop-blur-[18px]",
          "fixed top-0 z-[90] h-dvh w-[min(80vw,20rem)] -translate-x-full transition-transform duration-300",
          "bg-gradient-to-b from-[rgb(255_249_234/86%)] to-[rgb(245_231_197/78%)] dark:from-[rgb(34_32_25/92%)] dark:to-[rgb(26_24_20/95%)]",
          "lg:sticky lg:h-dvh lg:w-auto lg:translate-x-0",
          open && "translate-x-0",
        )}
        aria-label="Dashboard navigation"
      >
        <div className="flex items-center gap-3.5">
          <div className="grid h-[3.25rem] w-[3.25rem] place-items-center rounded-[1.1rem] border border-foreground/18 bg-foreground text-background font-display text-sm font-extrabold tracking-wide shadow-[0_14px_32px_rgb(24_33_28/18%)]">
            CT
          </div>
          <div>
            <p className="mb-0.5 font-display text-[0.74rem] font-extrabold uppercase tracking-[0.14em] text-primary">
              Plugin Web Program
            </p>
            <h1 className="m-0 font-display text-[1.1rem] font-bold tracking-tight">CodingTrajectory</h1>
          </div>
        </div>
        <nav className="grid gap-2">
          <Link to="/" className={navLinkClass} activeProps={{ className: "is-active" }} onClick={onClose}>
            <Sparkles size={18} /> Overview
          </Link>
          <Link to="/projects" className={navLinkClass} activeProps={{ className: "is-active" }} onClick={onClose}>
            <FolderGit2 size={18} /> Projects
          </Link>
          <Link to="/sessions" className={navLinkClass} activeProps={{ className: "is-active" }} onClick={onClose}>
            <Activity size={18} /> Sessions
          </Link>
        </nav>
        <div className="mt-auto grid gap-4">
          <button
            className="flex cursor-pointer items-center gap-2.5 rounded-full border border-foreground/13 bg-transparent px-3.5 py-2.5 font-display text-[0.85rem] font-bold text-muted-foreground hover:border-primary/24 hover:bg-primary/8 hover:text-foreground"
            onClick={onToggleTheme}
            aria-label="Toggle color theme"
          >
            {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            <span>{theme === "dark" ? "Light mode" : "Dark mode"}</span>
          </button>
          <p className="text-[0.95rem] leading-relaxed text-muted-foreground">
            Runs locally through <code>ct plugin dashboard web</code>. Destructive actions stay preview-first.
          </p>
        </div>
      </aside>
    </>
  );
}
