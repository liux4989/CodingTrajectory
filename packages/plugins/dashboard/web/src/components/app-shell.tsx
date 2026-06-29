import { Outlet } from "@tanstack/react-router";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "@/hooks/use-theme";
import { Button } from "@/components/ui/button";

export function AppShell() {
  const { theme, toggle } = useTheme();

  return (
    <main className="grid min-h-dvh grid-cols-1">
      <header className="sticky top-0 z-[90] flex items-center justify-between gap-4 border-b border-sidebar-border bg-[linear-gradient(135deg,rgb(255_249_234/94%),rgb(215_200_164/34%)),var(--paper-strong)] px-[clamp(1rem,2vw,2rem)] py-3 backdrop-blur-[18px] dark:border-border-subtle dark:bg-[linear-gradient(135deg,rgb(34_32_25/94%),rgb(58_54_44/34%)),var(--paper-strong)]">
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
        <Button
          variant="outline"
          size="icon"
          onClick={toggle}
          aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        >
          {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
        </Button>
      </header>
      <section className="min-w-0 p-[clamp(1rem,2vw,2rem)]">
        <Outlet />
      </section>
    </main>
  );
}
