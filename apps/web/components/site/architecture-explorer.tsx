"use client";

import { useState } from "react";
import { Blocks, Database, Layers, MonitorSmartphone, Waypoints } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { motion, useReducedMotion } from "framer-motion";

import { SpotlightGroup } from "@/components/site/spotlight-group";
import { architecture } from "@/lib/content";
import { cn } from "@/lib/utils";

const LAYER_ICONS: Record<string, LucideIcon> = {
  sources: Blocks,
  ingest: Layers,
  memory: Database,
  agents: Waypoints,
  experience: MonitorSmartphone,
};

type Layer = (typeof architecture.layers)[number];

/**
 * Layer picker + detail pane, wired as a proper ARIA tablist.
 *
 * The stack reads top-down as the data actually flows (sources → experience),
 * and selecting a layer swaps the detail pane rather than making the reader
 * scan five expanded blocks at once. Arrow keys move between layers, matching
 * native tab semantics.
 *
 * All five panels are rendered and the inactive ones carry `hidden`. Mounting
 * only the selected panel left the other four tabs pointing `aria-controls` at
 * ids that were not in the document, which is invalid for a tablist.
 */
export function ArchitectureExplorer() {
  const layers = architecture.layers as readonly Layer[];
  const [activeIndex, setActiveIndex] = useState(2); // open on the Memory core
  const shouldReduceMotion = useReducedMotion();

  const onKeyDown = (event: React.KeyboardEvent) => {
    const last = layers.length - 1;
    let next: number | null = null;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") next = activeIndex === last ? 0 : activeIndex + 1;
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") next = activeIndex === 0 ? last : activeIndex - 1;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = last;
    if (next === null) return;
    event.preventDefault();
    setActiveIndex(next);
    // Move focus with selection, as an ARIA tablist is expected to.
    document.getElementById(`arch-tab-${layers[next].id}`)?.focus();
  };

  return (
    <SpotlightGroup className="grid gap-6 lg:grid-cols-[minmax(0,19rem)_minmax(0,1fr)] lg:gap-8">
      <div
        role="tablist"
        aria-orientation="vertical"
        aria-label="Architecture layers"
        onKeyDown={onKeyDown}
        className="flex flex-col gap-2"
      >
        {layers.map((layer, index) => {
          const Icon = LAYER_ICONS[layer.id];
          const selected = index === activeIndex;
          return (
            <button
              key={layer.id}
              id={`arch-tab-${layer.id}`}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={`arch-panel-${layer.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => setActiveIndex(index)}
              className={cn(
                "group relative flex items-center gap-3 rounded-xl border px-4 py-3.5 text-left transition-colors duration-300",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
                selected
                  ? "border-brand/40 bg-brand/8"
                  : "spotlight border-hairline bg-surface/30 hover:border-hairline-strong",
              )}
            >
              <span
                className={cn(
                  "flex size-9 shrink-0 items-center justify-center rounded-lg border transition-colors duration-300",
                  selected
                    ? "border-brand/40 bg-brand/12 text-brand"
                    : "border-hairline-strong bg-background/60 text-muted-foreground group-hover:text-foreground",
                )}
              >
                <Icon aria-hidden="true" className="size-4.5" />
              </span>
              <span className="min-w-0">
                <span
                  className={cn(
                    "eyebrow block",
                    selected ? "text-brand" : "text-muted-foreground",
                  )}
                >
                  {layer.label}
                </span>
                <span className="mt-1 block font-mono text-[0.6875rem] text-muted-foreground/80">
                  {/* Every layer has 2+ nodes, so the plural is unconditional. */}
                  {layer.nodes.length} components
                </span>
              </span>
            </button>
          );
        })}
      </div>

      <div>
        {layers.map((layer, index) => {
          const selected = index === activeIndex;
          return (
            <div
              key={layer.id}
              id={`arch-panel-${layer.id}`}
              role="tabpanel"
              aria-labelledby={`arch-tab-${layer.id}`}
              hidden={!selected}
              tabIndex={selected ? 0 : -1}
              className="spotlight rounded-2xl border border-hairline-strong bg-surface/40 p-6 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand sm:p-8"
            >
              <motion.div
                // Re-keyed on selection so the fade replays each time a layer is
                // opened, even though the panel itself never unmounts.
                key={`${layer.id}-${String(selected)}`}
                initial={shouldReduceMotion ? false : { opacity: 0, y: 10 }}
                animate={shouldReduceMotion ? {} : { opacity: 1, y: 0 }}
                transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
              >
                <h3 className="display text-xl font-semibold sm:text-2xl">{layer.label}</h3>
                <p className="mt-2 text-pretty text-sm leading-relaxed text-muted-foreground">
                  {layer.summary}
                </p>

                <ul className="mt-6 grid gap-3 sm:grid-cols-2">
                  {layer.nodes.map((node) => (
                    <li
                      key={node.name}
                      className="rounded-xl border border-hairline bg-background/50 px-4 py-3 transition-colors duration-300 hover:border-brand/25"
                    >
                      <p className="text-sm font-medium tracking-tight text-foreground/90">
                        {node.name}
                      </p>
                      <p className="mt-1 font-mono text-[0.6875rem] leading-relaxed text-muted-foreground/80">
                        {node.detail}
                      </p>
                    </li>
                  ))}
                </ul>
              </motion.div>
            </div>
          );
        })}
      </div>
    </SpotlightGroup>
  );
}
