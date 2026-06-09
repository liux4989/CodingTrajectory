import * as React from "react";

type RouteHeaderProps = {
  eyebrow: string;
  title: string;
  action?: React.ReactNode;
};

export function RouteHeader({ eyebrow, title, action }: RouteHeaderProps) {
  return (
    <header className="flex items-start justify-between gap-4 rounded-[2rem] border border-foreground/13 bg-[linear-gradient(135deg,rgb(255_249_234/94%),rgb(215_200_164/34%)),var(--paper-strong)] p-[clamp(1rem,3vw,2.2rem)] shadow-[var(--shadow),0_24px_70px_rgb(49_42_25/18%)] dark:border-[rgb(255_255_255/8%)] dark:bg-[linear-gradient(135deg,rgb(34_32_25/94%),rgb(58_54_44/34%)),var(--paper-strong)] dark:shadow-[0_24px_70px_rgb(0_0_0/40%)]">
      <div>
        <p className="mb-1 font-display text-[0.74rem] font-extrabold uppercase tracking-[0.14em] text-primary">
          {eyebrow}
        </p>
        <h2 className="m-0 max-w-[18ch] font-display text-[clamp(2rem,5vw,5.25rem)] leading-[0.92] tracking-[-0.04em] text-wrap-balance">
          {title}
        </h2>
      </div>
      {action}
    </header>
  );
}
