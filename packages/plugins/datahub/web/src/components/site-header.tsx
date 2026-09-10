import * as React from "react";
import { Check, ChevronDown, CircleAlert, Cloud, ExternalLink, HardDrive, LoaderCircle, Moon, Sun } from "lucide-react";
import { useTheme } from "@/hooks/use-theme";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Breadcrumbs } from "@/components/breadcrumbs";
import { RefreshButton } from "@/components/refresh-button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useDatahubDelivery } from "@/hooks/use-datahub-delivery";
import { currentAppPath, sourceUrl, type DatahubSourceKind } from "@/lib/datahub-source";

function SourceSelector() {
  const delivery = useDatahubDelivery();
  const failed = delivery.sourceStatus?.failed ?? 0;
  const incomplete = delivery.sourceStatus?.incomplete ?? 0;
  const lag = delivery.freshness?.lag_seconds;
  const transportLabel = delivery.mode === "live" ? "Live" : delivery.mode === "reconnecting" ? "Reconnecting" : "Polling";
  const sourceLabel = delivery.profile?.label ?? "Source";
  const label = delivery.error
    ? "Delivery error"
    : delivery.catchingUp
      ? "Catching up"
      : failed > 0
      ? `${failed} source failure${failed === 1 ? "" : "s"}`
      : incomplete > 0
        ? `${incomplete} incomplete source${incomplete === 1 ? "" : "s"}`
        : `${sourceLabel} · ${transportLabel}`;
  const detail = delivery.error
    ? `Delivery unavailable: ${delivery.error}`
    : `${delivery.transport ? `Shared revision ${delivery.transport.snapshot_sequence}` : "Local sources"} · Revision ${delivery.revision ?? "—"} · ${lag == null ? "refresh lag unavailable" : `${Math.round(lag)}s refresh lag`}`;

  const selectSource = (kind: DatahubSourceKind) => {
    if (delivery.profile?.kind === kind) return;
    const target = sourceUrl(kind, currentAppPath());
    if (kind === "local") window.open(target, "_blank", "noopener,noreferrer");
    else window.location.assign(target);
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" disabled={delivery.profile == null}>
          {delivery.catchingUp || delivery.isRefreshing || delivery.mode === "reconnecting" ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : null}
          {delivery.error || failed > 0 ? <CircleAlert data-icon="inline-start" /> : null}
          {!delivery.error && failed === 0 ? (delivery.profile?.kind === "remote" ? <Cloud data-icon="inline-start" /> : <HardDrive data-icon="inline-start" />) : null}
          <span className="hidden sm:inline">{label}</span>
          <ChevronDown data-icon="inline-end" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72">
        <DropdownMenuLabel>Data source</DropdownMenuLabel>
        <DropdownMenuGroup>
          <DropdownMenuItem onSelect={() => selectSource("local")} aria-current={delivery.profile?.kind === "local" ? "true" : undefined}>
            <HardDrive />
            <span className="grid flex-1 gap-0.5">
              <span className="font-medium">Local</span>
              <span className="text-caption text-muted-foreground">Live sources and full analysis</span>
            </span>
            {delivery.profile?.kind === "local" ? <Check /> : delivery.profile?.kind === "remote" ? <ExternalLink /> : null}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => selectSource("remote")} aria-current={delivery.profile?.kind === "remote" ? "true" : undefined}>
            <Cloud />
            <span className="grid flex-1 gap-0.5">
              <span className="font-medium">Shared</span>
              <span className="text-caption text-muted-foreground">Live committed workspace data</span>
            </span>
            {delivery.profile?.kind === "remote" ? <Check /> : null}
          </DropdownMenuItem>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <p className="px-2 py-1.5 text-caption text-muted-foreground">{detail}</p>
      </DropdownMenuContent>
    </DropdownMenu>
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
          <SourceSelector />
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
