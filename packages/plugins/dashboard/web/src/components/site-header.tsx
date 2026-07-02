import * as React from "react";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "@/hooks/use-theme";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { DateRangeToggle } from "@/components/date-range-toggle";
import { Breadcrumbs } from "@/components/breadcrumbs";

export function SiteHeader() {
  const { theme, toggle } = useTheme();

  return (
    <header className="flex h-16 shrink-0 items-center gap-2 border-b border-sidebar-border bg-background/80 backdrop-blur-[18px] transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-16">
      <div className="flex w-full items-center gap-1 px-4 lg:gap-2 lg:px-6">
        <SidebarTrigger className="-ml-1" />
        <Separator
          orientation="vertical"
          className="mx-2 data-[orientation=vertical]:h-4"
        />
        <Breadcrumbs />
        <div className="ml-auto flex items-center gap-2">
          <DateRangeToggle />
          <Button
            variant="outline"
            size="icon"
            onClick={toggle}
            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          >
            {theme === "dark" ? <Sun /> : <Moon />}
          </Button>
        </div>
      </div>
    </header>
  );
}
