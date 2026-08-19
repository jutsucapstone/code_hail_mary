"use client";

import type { ComponentProps } from "react";
import { ThemeProvider as NextThemesProvider } from "next-themes";

/**
 * `next-themes` writes the `.dark` class onto <html> before paint via an inline
 * script, which is what keeps the first frame from flashing the wrong theme.
 * It pairs with shadcn's class-based dark variant (`&:is(.dark *))`.
 */
export function ThemeProvider({ children, ...props }: ComponentProps<typeof NextThemesProvider>) {
  return <NextThemesProvider {...props}>{children}</NextThemesProvider>;
}
