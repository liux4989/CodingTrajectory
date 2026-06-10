import { cn } from "@/lib/utils";

type StatCardProps = {
  label: string;
  value: string;
  detail?: string;
  className?: string;
};

export function StatCard({ label, value, detail, className }: StatCardProps) {
  return (
    <div
      className={cn(
        "rounded-xl border border-border bg-card p-5 shadow-sm",
        className,
      )}
    >
      <p className="text-eyebrow font-display uppercase tracking-wider text-muted-foreground">
        {label}
      </p>
      <p className="mt-1 font-display text-metric font-semibold tracking-tight text-foreground">
        {value}
      </p>
      {detail && (
        <p className="mt-1 text-caption text-muted-foreground">{detail}</p>
      )}
    </div>
  );
}
