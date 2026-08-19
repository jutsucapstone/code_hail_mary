import type { ReactNode } from "react";
import { ArrowRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * The shadcn `nova` preset ships compact control heights tuned for dense app
 * chrome (h-8 / h-9). Marketing CTAs need a bigger tap target, so both wrappers
 * override height and padding once, here, instead of at every call site.
 */
export function PrimaryCta({
  href,
  children,
  className,
}: {
  href: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Button
      asChild
      size="lg"
      className={cn(
        "group h-12 rounded-xl bg-brand px-6 text-[0.9375rem] font-semibold text-brand-foreground",
        "shadow-[0_10px_36px_-14px_color-mix(in_oklab,var(--brand)_70%,transparent)]",
        "hover:bg-brand/90 focus-visible:ring-brand/40",
        className,
      )}
    >
      <a href={href}>
        {children}
        <ArrowRight
          aria-hidden="true"
          className="transition-transform duration-300 group-hover:translate-x-0.5"
        />
      </a>
    </Button>
  );
}

export function SecondaryCta({
  href,
  children,
  className,
}: {
  href: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Button
      asChild
      variant="outline"
      size="lg"
      className={cn(
        "h-12 rounded-xl border-hairline-strong bg-transparent px-6 text-[0.9375rem] font-medium",
        "hover:border-brand/40 hover:bg-brand/5 hover:text-foreground",
        "dark:border-hairline-strong dark:bg-transparent dark:hover:bg-brand/5",
        className,
      )}
    >
      <a href={href}>{children}</a>
    </Button>
  );
}
