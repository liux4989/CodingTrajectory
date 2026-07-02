import * as React from "react";
import { useQueryClient, useIsFetching } from "@tanstack/react-query";
import { RefreshCcw } from "lucide-react";
import { motion } from "motion/react";
import { Button } from "@/components/ui/button";

export function RefreshButton({ queries }: { queries: string[] }) {
  const client = useQueryClient();
  const isFetching = useIsFetching({ queryKey: queries.length ? [queries[0]] : undefined });
  const spinning = isFetching > 0;
  return (
    <Button
      variant="secondary"
      onClick={() => queries.forEach((query) => void client.invalidateQueries({ queryKey: [query] }))}
    >
      <motion.span
        animate={spinning ? { rotate: 360 } : { rotate: 0 }}
        transition={spinning ? { repeat: Infinity, ease: "linear", duration: 0.9 } : { duration: 0.2 }}
        className="inline-flex"
      >
        <RefreshCcw size={16} />
      </motion.span>{" "}
      Refresh
    </Button>
  );
}
