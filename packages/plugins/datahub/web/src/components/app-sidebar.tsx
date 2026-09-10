import * as React from "react";
import { Link, useMatchRoute } from "@tanstack/react-router";
import {
  CalendarDays,
  ExternalLink,
  GitCompareArrows,
  MessageSquare,
  Timer,
  type LucideIcon,
} from "lucide-react";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { Badge } from "@/components/ui/badge";
import { useDatahubDelivery } from "@/hooks/use-datahub-delivery";
import { hasCapability, sourceUrl, type DatahubCapability } from "@/lib/datahub-source";

type NavItem = {
  title: string;
  url: string;
  icon: LucideIcon;
  match: () => boolean;
  capability: DatahubCapability;
};

type NavGroup = {
  label: string;
  items: NavItem[];
};

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  const matchRoute = useMatchRoute();
  const { profile } = useDatahubDelivery();

  const observe: NavGroup = {
    label: "Observe",
    items: [
      {
        title: "Sessions",
        url: "/sessions",
        icon: MessageSquare,
        match: () => Boolean(matchRoute({ to: "/sessions", fuzzy: true })),
        capability: "sessions",
      },
      {
        title: "Today",
        url: "/today",
        icon: CalendarDays,
        match: () => Boolean(matchRoute({ to: "/today" })),
        capability: "today",
      },
    ],
  };
  const groups: NavGroup[] = [
    observe,
    {
      label: "Analyze",
      items: [
        {
          title: "Compare",
          url: "/compare",
          icon: GitCompareArrows,
          match: () => Boolean(matchRoute({ to: "/compare" })),
          capability: "compare",
        },
        {
          title: "Code Time",
          url: "/code-time",
          icon: Timer,
          match: () => Boolean(matchRoute({ to: "/code-time" })),
          capability: "code-time",
        },
      ],
    },
  ];

  return (
    <Sidebar collapsible="icon" {...props}>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              asChild
              tooltip="Session observability"
              className="data-[slot=sidebar-menu-button]:p-1.5!"
            >
              <Link to="/sessions" search={{ projectName: undefined }} preload="intent">
                <div className="grid aspect-square size-8 place-items-center rounded-md border border-sidebar-border bg-sidebar-primary text-sidebar-primary-foreground font-display text-xs font-extrabold tracking-wide">
                  CT
                </div>
                <div className="grid flex-1 text-left text-sm leading-tight">
                  <span className="truncate font-display font-bold">CodingTrajectory</span>
                </div>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        {groups.map((group) => (
          <SidebarGroup key={group.label}>
            <SidebarGroupLabel>{group.label}</SidebarGroupLabel>
            <SidebarGroupContent className="flex flex-col gap-1">
              <SidebarMenu>
                {group.items.map((item) => {
                  const active = item.match();
                  const available = hasCapability(profile, item.capability);
                  const content = (
                    <>
                      <item.icon />
                      <span>{item.title}</span>
                      {!available ? (
                        <Badge variant="secondary" className="ml-auto group-data-[collapsible=icon]:hidden">
                          Local
                        </Badge>
                      ) : null}
                      {!available ? <ExternalLink className="group-data-[collapsible=icon]:hidden" /> : null}
                    </>
                  );
                  return (
                    <SidebarMenuItem key={item.url}>
                      <SidebarMenuButton
                        asChild
                        isActive={active}
                        tooltip={available ? item.title : `${item.title} · Open in Local`}
                      >
                        {available ? (
                          <Link to={item.url} preload="intent">{content}</Link>
                        ) : (
                          <a href={sourceUrl("local", item.url)} target="_blank" rel="noreferrer">
                            {content}
                          </a>
                        )}
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}
      </SidebarContent>
    </Sidebar>
  );
}
