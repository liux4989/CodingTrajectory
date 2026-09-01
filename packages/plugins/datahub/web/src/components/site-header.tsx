import * as React from "react";
import { CircleAlert, LoaderCircle, Moon, Sun } from "lucide-react";
import { useTheme } from "@/hooks/use-theme";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Breadcrumbs } from "@/components/breadcrumbs";
import { RefreshButton } from "@/components/refresh-button";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useDatahubDelivery } from "@/hooks/use-datahub-delivery";

function DeliveryStatus() {
  const delivery = useDatahubDelivery();
  const failed = delivery.sourceStatus?.failed ?? 0;
  const incomplete = delivery.sourceStatus?.incomplete ?? 0;
  const lag = delivery.freshness?.lag_seconds;
  const transportLabel = delivery.mode === "live" ? "Live" : delivery.mode === "reconnecting" ? "Reconnecting" : "Polling";
  const label = delivery.error
    ? "Delivery error"
    : delivery.catchingUp
      ? "Catching up"
      : failed > 0
      ? `${failed} source failure${failed === 1 ? "" : "s"}`
      : incomplete > 0
        ? `${incomplete} incomplete source${incomplete === 1 ? "" : "s"}`
        : transportLabel;
  const detail = delivery.error
    ? `Delivery unavailable: ${delivery.error}`
    : `Revision ${delivery.revision ?? "—"} · ${lag == null ? "refresh lag unavailable" : `${Math.round(lag)}s refresh lag`}`;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant={delivery.error || failed > 0 ? "destructive" : "outline"} className="hidden gap-1 sm:inline-flex">
          {delivery.catchingUp || delivery.isRefreshing || delivery.mode === "reconnecting" ? <LoaderCircle className="animate-spin" /> : null}
          {delivery.error || failed > 0 ? <CircleAlert /> : null}
          {label}
        </Badge>
      </TooltipTrigger>
      <TooltipContent>{detail}</TooltipContent>
    </Tooltip>
  );
}

export function SiteHeader() {
  const { theme, toggle } = useTheme();

  return (
    <header className="flex h-16 shrink-0 items-center gap-2 border-b border-sidebar-border bg-background/80 backdrop-blur-lg transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-16">
      <div className="flex w-full items-center gap-1 px-4 lg:gap-2 lg:px-6">
        <SidebarTrigger className="-ml-1" />
        <Separator
          orientation="vertical"
          className="mx-2 data-[orientation=vertical]:h-4"
        />
        <Breadcrumbs />
        <div className="ml-auto flex items-center gap-2">
          <DeliveryStatus />
          <RefreshButton />
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
