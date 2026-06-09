import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { RefreshCcw } from "lucide-react";
import { Button } from "@/components/ui/button";

export function RefreshButton({ queries }: { queries: string[] }) {
  const client = useQueryClient();
  return (
    <Button variant="secondary" onClick={() => queries.forEach((query) => void client.invalidateQueries({ queryKey: [query] }))}>
      <RefreshCcw size={16} /> Refresh
    </Button>
  );
}
