import * as React from "react";
import { Input } from "@/components/ui/input";

type ToolbarProps = {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
};

export function Toolbar({ value, onChange, placeholder }: ToolbarProps) {
  return (
    <form className="grid max-w-[44rem] gap-2" role="search" onSubmit={(event) => event.preventDefault()}>
      <label htmlFor="route-filter" className="font-display text-caption font-extrabold uppercase tracking-wide text-muted-foreground">
        Filter
      </label>
      <Input id="route-filter" name="filter" value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} autoComplete="off" />
    </form>
  );
}
