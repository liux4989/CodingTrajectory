import * as React from "react";
import { Link, Outlet } from "@tanstack/react-router";
import { Activity, FolderGit2, Moon, Sparkles, Sun, Menu, X } from "lucide-react";
import { useTheme } from "../hooks/use-theme";

export function AppShell() {
  const { theme, toggle } = useTheme();
  const [sidebarOpen, setSidebarOpen] = React.useState(false);

  return (
    <main className="app-shell">
      <button
        className="sidebar-toggle"
        onClick={() => setSidebarOpen((open) => !open)}
        aria-label={sidebarOpen ? "Close navigation" : "Open navigation"}
      >
        {sidebarOpen ? <X size={22} /> : <Menu size={22} />}
      </button>
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} theme={theme} onToggleTheme={toggle} />
      <section className="content-stage">
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

function Sidebar({ open, onClose, theme, onToggleTheme }: SidebarProps) {
  return (
    <>
      {open ? <div className="sidebar-backdrop" onClick={onClose} /> : null}
      <aside className={`side-rail ${open ? "is-open" : ""}`} aria-label="Dashboard navigation">
        <div className="brand-lockup">
          <div className="brand-mark">CT</div>
          <div>
            <p className="eyebrow">Plugin Web Program</p>
            <h1>CodingTrajectory</h1>
          </div>
        </div>
        <nav className="nav-list">
          <Link to="/" className="nav-link" activeProps={{ className: "nav-link is-active" }} onClick={onClose}>
            <Sparkles size={18} /> Overview
          </Link>
          <Link to="/projects" className="nav-link" activeProps={{ className: "nav-link is-active" }} onClick={onClose}>
            <FolderGit2 size={18} /> Projects
          </Link>
          <Link to="/sessions" className="nav-link" activeProps={{ className: "nav-link is-active" }} onClick={onClose}>
            <Activity size={18} /> Sessions
          </Link>
        </nav>
        <div className="sidebar-footer">
          <button className="theme-toggle" onClick={onToggleTheme} aria-label="Toggle color theme">
            {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            <span>{theme === "dark" ? "Light mode" : "Dark mode"}</span>
          </button>
          <p className="side-note">
            Runs locally through <code>ct plugin dashboard web</code>. Destructive actions stay preview-first.
          </p>
        </div>
      </aside>
    </>
  );
}
