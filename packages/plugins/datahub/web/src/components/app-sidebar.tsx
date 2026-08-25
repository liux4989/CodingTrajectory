import * as React from "react";
import { Link, useMatchRoute } from "@tanstack/react-router";
import {
  CalendarDays,
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

type NavItem = {
  title: string;
  url: string;
  icon: LucideIcon;
  match: () => boolean;
};

type NavGroup = {
  label: string;
  items: NavItem[];
};

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  const matchRoute = useMatchRoute();

  const groups: NavGroup[] = [
    {
      label: "Workspace",
      items: [
        {
          title: "Sessions",
          url: "/sessions",
          icon: MessageSquare,
          match: () => Boolean(matchRoute({ to: "/sessions", fuzzy: true })),
        },
        {
          title: "Today",
          url: "/today",
          icon: CalendarDays,
          match: () => Boolean(matchRoute({ to: "/today" })),
        },
        {
          title: "Compare",
          url: "/compare",
          icon: GitCompareArrows,
          match: () => Boolean(matchRoute({ to: "/compare" })),
        },
        {
          title: "Code Time",
          url: "/code-time",
          icon: Timer,
          match: () => Boolean(matchRoute({ to: "/code-time" })),
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
              className="data-[slot=sidebar-menu-button]:p-1.5!"
            >
              <Link to="/sessions" search={{ projectName: undefined }} preload="intent">
                <div className="grid aspect-square size-8 place-items-center rounded-md border border-sidebar-border bg-sidebar-primary text-sidebar-primary-foreground font-display text-xs font-extrabold tracking-wide">
                  CT
                </div>
                <div className="grid flex-1 text-left text-sm leading-tight">
                  <span className="truncate font-display font-bold">CodingTrajectory</span>
                  <span className="truncate text-xs text-muted-foreground">Session observability</span>
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
                  return (
                    <SidebarMenuItem key={item.url}>
                      <SidebarMenuButton
                        asChild
                        isActive={active}
                        tooltip={item.title}
                      >
                        <Link to={item.url} preload="intent">
                          <item.icon />
                          <span>{item.title}</span>
                        </Link>
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
