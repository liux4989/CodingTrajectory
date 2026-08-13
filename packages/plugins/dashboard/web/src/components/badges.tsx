import * as React from "react";
import { Badge } from "@/components/ui/badge";

export function VendorBadges({ vendors }: { vendors: string[] }) {
  if (!vendors.length) return <span className="text-muted-foreground">-</span>;
  return (
    <div className="flex flex-wrap gap-2">
      {vendors.map((vendor) => (
        <Badge key={vendor} variant="secondary">{vendor}</Badge>
      ))}
    </div>
  );
}

