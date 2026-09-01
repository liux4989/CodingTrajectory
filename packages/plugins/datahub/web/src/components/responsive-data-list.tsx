import type { ReactNode } from "react";

export function ResponsiveDataList({ table, cards }: { table: ReactNode; cards: ReactNode }) {
  return (
    <>
      <div className="hidden min-[48rem]:block">{table}</div>
      <div className="grid gap-2 min-[48rem]:hidden">{cards}</div>
    </>
  );
}
