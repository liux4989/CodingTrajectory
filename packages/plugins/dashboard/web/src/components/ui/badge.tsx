import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";

const badgeVariants = cva("badge", {
  variants: {
    variant: {
      default: "badge-default",
      quiet: "badge-quiet",
      risk: "badge-risk",
    },
  },
  defaultVariants: {
    variant: "default",
  },
});

export type BadgeProps = React.HTMLAttributes<HTMLSpanElement> &
  VariantProps<typeof badgeVariants>;

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
