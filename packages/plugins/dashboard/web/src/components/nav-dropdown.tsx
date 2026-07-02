import * as React from "react";
import { Link } from "@tanstack/react-router";
import { ChevronDown } from "lucide-react";
import { DropdownMenu } from "radix-ui";
import { cn } from "@/lib/utils";

export type NavDropdownItem = {
  to: string;
  label: string;
  description?: string;
};

type Props = {
  label: string;
  items: NavDropdownItem[];
  active: boolean;
  className?: string;
};

export function NavDropdown({ label, items, active, className }: Props) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex items-center gap-1 rounded-md px-3 py-1.5 font-medium transition-colors",
            active
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:text-foreground",
            className,
          )}
        >
          {label}
          <ChevronDown size={14} className={opacityClass(active)} />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={8}
          className="z-50 min-w-[16rem] rounded-lg border border-border-subtle bg-popover p-1 text-popover-foreground shadow-md"
        >
          {items.map((item) => (
            <DropdownMenu.Item key={item.to} asChild>
              <Link
                to={item.to}
                preload="intent"
                className="flex cursor-pointer flex-col gap-0.5 rounded-md px-3 py-2 text-body-sm outline-none data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground"
              >
                <span className="font-medium">{item.label}</span>
                {item.description ? (
                  <span className="text-caption text-muted-foreground">{item.description}</span>
                ) : null}
              </Link>
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function opacityClass(active: boolean) {
  return active ? "opacity-100" : "opacity-70";
}
