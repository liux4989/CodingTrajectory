import * as React from "react";
import { Input } from "./ui/input";

type ToolbarProps = {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
};

export function Toolbar({ value, onChange, placeholder }: ToolbarProps) {
  return (
    <form className="toolbar" role="search" onSubmit={(event) => event.preventDefault()}>
      <label htmlFor="route-filter">Filter</label>
      <Input id="route-filter" name="filter" value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} autoComplete="off" />
    </form>
  );
}
