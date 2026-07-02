import { Outlet, useLocation } from "@tanstack/react-router";
import { motion } from "motion/react";
import { AppSidebar } from "@/components/app-sidebar";
import { SiteHeader } from "@/components/site-header";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

export function AppShell() {
  const location = useLocation();
  return (
    <SidebarProvider
      style={
        {
          "--sidebar-width": "calc(var(--spacing) * 60)",
          "--header-height": "calc(var(--spacing) * 12)",
        } as React.CSSProperties
      }
    >
      <AppSidebar variant="inset" />
      <SidebarInset>
        <SiteHeader />
        <main className="flex flex-1 flex-col">
          <motion.div
            key={location.pathname}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.34, ease: [0.22, 1, 0.36, 1] }}
            className="@container/main flex flex-1 flex-col gap-4 p-[clamp(1rem,2vw,2rem)] md:gap-6"
          >
            <Outlet />
          </motion.div>
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}
