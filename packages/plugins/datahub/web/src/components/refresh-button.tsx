import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { RefreshCcw } from "lucide-react";
import { toast } from "sonner";
import { motion } from "motion/react";
import { refreshDatahubData } from "@/api";
import { Button } from "@/components/ui/button";

export function RefreshButton() {
  const client = useQueryClient();
  const [isRefreshing, setIsRefreshing] = React.useState(false);

  async function refresh() {
    setIsRefreshing(true);
    try {
      await refreshDatahubData();
      await client.invalidateQueries();
      toast.success("Datahub data refreshed");
    } catch (error) {
      toast.error(`Refresh failed: ${error instanceof Error ? error.message : "Unknown error"}`);
    } finally {
      setIsRefreshing(false);
    }
  }

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => void refresh()}
      disabled={isRefreshing}
    >
      <motion.span
        animate={isRefreshing ? { rotate: 360 } : { rotate: 0 }}
        transition={isRefreshing ? { repeat: Infinity, ease: "linear", duration: 0.9 } : { duration: 0.2 }}
        className="inline-flex"
      >
        <RefreshCcw size={16} />
      </motion.span>{" "}
      Refresh
    </Button>
  );
}
