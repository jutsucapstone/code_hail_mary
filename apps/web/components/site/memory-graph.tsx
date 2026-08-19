"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "framer-motion";

import { cn } from "@/lib/utils";

type NodeKind = "person" | "project" | "decision" | "skill";

interface GraphNode {
  id: string;
  label: string;
  kind: NodeKind;
  /** What this node "knows" — surfaced in the readout when it is active. */
  note: string;
  x: number;
  y: number;
  r: number;
  /** Labels flip to the left near the right edge so they never clip. */
  labelSide?: "left" | "right";
}

/**
 * Laid out to fill a 660×500 frame — close to the panel's own aspect, so the
 * constellation uses the whole card instead of hugging the top third.
 */
const NODES: GraphNode[] = [
  { id: "p3", label: "Meera", kind: "person", note: "Joined 4 months ago; ramped on Atlas.", x: 168, y: 78, r: 6 },
  { id: "p1", label: "Priya", kind: "person", note: "Sole owner on 3 critical projects — bus factor 1.", x: 86, y: 214, r: 7.5 },
  { id: "p2", label: "Arjun", kind: "person", note: "Kubernetes cost work across Falcon and Atlas.", x: 132, y: 402, r: 6.5 },
  { id: "pr1", label: "Falcon", kind: "project", note: "14 decisions, 3 owners, 22 linked meetings.", x: 312, y: 236, r: 13 },
  { id: "pr2", label: "Atlas", kind: "project", note: "Depends on the Falcon storage decision.", x: 474, y: 118, r: 10 },
  { id: "d1", label: "Postgres over Mongo", kind: "decision", note: "Decided 12 Mar — owner Priya, cited to 2 meetings.", x: 452, y: 342, r: 8.5 },
  { id: "d2", label: "Ship read-only", kind: "decision", note: "Decided 4 Apr — supersedes the write-back plan.", x: 588, y: 224, r: 7.5, labelSide: "left" },
  { id: "s1", label: "Kubernetes", kind: "skill", note: "2 contributors ranked by real contributions.", x: 196, y: 330, r: 5.5 },
  { id: "s3", label: "pgvector", kind: "skill", note: "Introduced by the Postgres decision.", x: 348, y: 438, r: 5 },
  { id: "s2", label: "GraphRAG", kind: "skill", note: "Emerged from the retrieval decisions on Atlas.", x: 596, y: 404, r: 5.5, labelSide: "left" },
];

/** `bow` bends each edge; alternating signs keep the web organic, not radial. */
const EDGES: Array<{ a: string; b: string; bow: number }> = [
  { a: "p1", b: "pr1", bow: 0.13 },
  { a: "p1", b: "s1", bow: -0.1 },
  { a: "p2", b: "s1", bow: 0.12 },
  { a: "p2", b: "pr1", bow: -0.09 },
  { a: "p3", b: "pr2", bow: 0.1 },
  { a: "p3", b: "pr1", bow: -0.12 },
  { a: "pr1", b: "pr2", bow: 0.11 },
  { a: "pr1", b: "d1", bow: -0.12 },
  { a: "pr2", b: "d2", bow: 0.13 },
  { a: "d1", b: "d2", bow: -0.11 },
  { a: "d1", b: "s3", bow: 0.12 },
  { a: "d2", b: "s2", bow: -0.13 },
  { a: "s3", b: "pr1", bow: 0.1 },
  { a: "s2", b: "d1", bow: 0.12 },
];

/** Edges that carry a travelling packet — the live query path. */
const FLOWS = [
  { edge: 0, offset: 0 },
  { edge: 7, offset: 0.33 },
  { edge: 9, offset: 0.66 },
  { edge: 8, offset: 0.15 },
  { edge: 11, offset: 0.5 },
];

const KIND_COLOR: Record<NodeKind, string> = {
  person: "var(--brand)",
  project: "var(--foreground)",
  decision: "var(--graph)",
  skill: "var(--graph-muted)",
};

const KIND_LABEL: Record<NodeKind, string> = {
  person: "Person",
  project: "Project",
  decision: "Decision",
  skill: "Skill",
};

const INDEX = new Map(NODES.map((n, i) => [n.id, i]));
const byId = (id: string) => NODES[INDEX.get(id)!];

/** Quadratic control point: midpoint pushed along the edge normal. */
function control(ax: number, ay: number, bx: number, by: number, bow: number) {
  const mx = (ax + bx) / 2;
  const my = (ay + by) / 2;
  return { cx: mx - (by - ay) * bow, cy: my + (bx - ax) * bow };
}

const quad = (p0: number, p1: number, p2: number, t: number) => {
  const u = 1 - t;
  return u * u * p0 + 2 * u * t * p1 + t * t * p2;
};

/**
 * The memory graph, drawn as a living system rather than a diagram.
 *
 * Richness comes from four things working off one set of coordinates:
 *  - node *shape* encodes type (disc / squircle / diamond / ring), so the graph
 *    is readable without relying on colour alone;
 *  - edges are quadratic curves, bowed off-axis, which reads as a web instead
 *    of a wheel of straight spokes;
 *  - packets travel those curves, showing retrieval actually moving through the
 *    graph rather than a static "query path" caption;
 *  - a slow drift keeps everything breathing.
 *
 * All of it is driven from a single rAF loop writing SVG attributes directly:
 * 10 nodes at 60fps of React re-renders would be waste, and edges, packets and
 * labels must all move in the *same* frame as their nodes or the graph tears.
 * The loop suspends off-screen and never starts under `prefers-reduced-motion`.
 * Drift phases derive from the node index — no `Math.random`, so SSR matches.
 */
export function MemoryGraph({ className, hint }: { className?: string; hint?: string }) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const shouldReduceMotion = useReducedMotion();

  const svgRef = useRef<SVGSVGElement | null>(null);
  const nodeRefs = useRef<Array<SVGGElement | null>>([]);
  const edgeRefs = useRef<Array<SVGPathElement | null>>([]);
  const glowRefs = useRef<Array<SVGPathElement | null>>([]);
  const packetRefs = useRef<Array<SVGCircleElement | null>>([]);

  const neighbours = useMemo(() => {
    if (!activeId) return null;
    const set = new Set<string>([activeId]);
    for (const e of EDGES) {
      if (e.a === activeId) set.add(e.b);
      if (e.b === activeId) set.add(e.a);
    }
    return set;
  }, [activeId]);

  const active = activeId ? byId(activeId) : null;
  const isDimmed = (id: string) => neighbours !== null && !neighbours.has(id);
  const isEdgeActive = (a: string, b: string) =>
    Boolean(activeId) && (a === activeId || b === activeId);

  useEffect(() => {
    if (shouldReduceMotion) return;
    const svg = svgRef.current;
    if (!svg) return;

    let raf = 0;
    let running = false;

    const drift = NODES.map((_, i) => ({
      ax: 5 + (i % 3) * 2.4,
      ay: 4 + ((i + 1) % 4) * 1.9,
      sx: 0.00021 + (i % 5) * 0.00004,
      sy: 0.00027 + (i % 4) * 0.00005,
      px: i * 1.7,
      py: i * 2.3,
    }));
    const pos = NODES.map((n) => ({ x: n.x, y: n.y }));

    const tick = (t: number) => {
      for (let i = 0; i < NODES.length; i++) {
        const d = drift[i];
        pos[i].x = NODES[i].x + Math.sin(t * d.sx + d.px) * d.ax;
        pos[i].y = NODES[i].y + Math.cos(t * d.sy + d.py) * d.ay;
        const g = nodeRefs.current[i];
        if (g) {
          g.setAttribute(
            "transform",
            `translate(${(pos[i].x - NODES[i].x).toFixed(2)} ${(pos[i].y - NODES[i].y).toFixed(2)})`,
          );
        }
      }

      for (let i = 0; i < EDGES.length; i++) {
        const e = EDGES[i];
        const a = pos[INDEX.get(e.a)!];
        const b = pos[INDEX.get(e.b)!];
        const { cx, cy } = control(a.x, a.y, b.x, b.y, e.bow);
        const d = `M${a.x.toFixed(1)} ${a.y.toFixed(1)}Q${cx.toFixed(1)} ${cy.toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
        edgeRefs.current[i]?.setAttribute("d", d);
        glowRefs.current[i]?.setAttribute("d", d);
      }

      for (let i = 0; i < FLOWS.length; i++) {
        const dot = packetRefs.current[i];
        if (!dot) continue;
        const e = EDGES[FLOWS[i].edge];
        const a = pos[INDEX.get(e.a)!];
        const b = pos[INDEX.get(e.b)!];
        const { cx, cy } = control(a.x, a.y, b.x, b.y, e.bow);
        const p = ((t / 3600 + FLOWS[i].offset) % 1 + 1) % 1;
        // Ease the packet so it slows into the target node, then restarts.
        const eased = p < 0.85 ? p / 0.85 : 1;
        dot.setAttribute("cx", quad(a.x, cx, b.x, eased).toFixed(1));
        dot.setAttribute("cy", quad(a.y, cy, b.y, eased).toFixed(1));
        dot.setAttribute("opacity", String(p < 0.85 ? Math.sin(p / 0.85 * Math.PI) * 0.9 : 0));
      }

      raf = requestAnimationFrame(tick);
    };

    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && !running) {
          running = true;
          raf = requestAnimationFrame(tick);
        } else if (!entry.isIntersecting && running) {
          running = false;
          cancelAnimationFrame(raf);
        }
      },
      { rootMargin: "120px" },
    );
    io.observe(svg);

    return () => {
      io.disconnect();
      cancelAnimationFrame(raf);
    };
  }, [shouldReduceMotion]);

  const onNodeKey = useCallback((event: React.KeyboardEvent, id: string) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setActiveId((cur) => (cur === id ? null : id));
    }
    if (event.key === "Escape") setActiveId(null);
  }, []);

  const staticPath = (e: (typeof EDGES)[number]) => {
    const a = byId(e.a);
    const b = byId(e.b);
    const { cx, cy } = control(a.x, a.y, b.x, b.y, e.bow);
    return `M${a.x} ${a.y}Q${cx} ${cy} ${b.x} ${b.y}`;
  };

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      <svg
        ref={svgRef}
        viewBox="0 0 660 500"
        aria-label="Interactive knowledge graph linking people to projects, projects to decisions, and decisions to skills."
        className="h-full w-full flex-1 overflow-visible"
        onMouseLeave={() => setActiveId(null)}
      >
        <defs>
          <radialGradient id="jutsu-graph-halo" cx="46%" cy="46%" r="58%">
            <stop offset="0%" stopColor="var(--brand)" stopOpacity="0.14" />
            <stop offset="55%" stopColor="var(--graph)" stopOpacity="0.05" />
            <stop offset="100%" stopColor="var(--brand)" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="jutsu-graph-edge" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--brand)" stopOpacity="0.55" />
            <stop offset="100%" stopColor="var(--graph)" stopOpacity="0.55" />
          </linearGradient>
          <filter id="jutsu-graph-bloom" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="4" />
          </filter>
        </defs>

        <rect x="0" y="0" width="660" height="500" fill="url(#jutsu-graph-halo)" />

        {/* Bloom pass: a blurred copy under the crisp edges, for depth. */}
        <g fill="none" stroke="var(--brand)" filter="url(#jutsu-graph-bloom)">
          {EDGES.map((e, i) => (
            <path
              key={`glow-${e.a}-${e.b}`}
              ref={(el) => {
                glowRefs.current[i] = el;
              }}
              d={staticPath(e)}
              strokeWidth={isEdgeActive(e.a, e.b) ? 3 : 0}
              strokeOpacity={isEdgeActive(e.a, e.b) ? 0.5 : 0}
              className="transition-[stroke-opacity,stroke-width] duration-300"
            />
          ))}
        </g>

        <g fill="none">
          {EDGES.map((e, i) => {
            const lit = isEdgeActive(e.a, e.b);
            return (
              <path
                key={`${e.a}-${e.b}`}
                ref={(el) => {
                  edgeRefs.current[i] = el;
                }}
                d={staticPath(e)}
                stroke={lit ? "var(--brand)" : "url(#jutsu-graph-edge)"}
                strokeWidth={lit ? 2 : 1}
                strokeOpacity={activeId ? (lit ? 1 : 0.08) : 0.42}
                strokeLinecap="round"
                className="transition-[stroke-opacity,stroke-width] duration-300"
              />
            );
          })}
        </g>

        {/* Packets: retrieval moving through the graph, not a static caption. */}
        <g className={cn("transition-opacity duration-300", activeId && "opacity-0")}>
          {FLOWS.map((f, i) => (
            <circle
              key={`packet-${i}`}
              ref={(el) => {
                packetRefs.current[i] = el;
              }}
              r="3"
              fill="var(--brand)"
              opacity="0"
            />
          ))}
        </g>

        <g>
          {NODES.map((node, index) => {
            const dimmed = isDimmed(node.id);
            const isActive = node.id === activeId;
            const color = KIND_COLOR[node.kind];
            const left = node.labelSide === "left";
            const r = node.r;

            return (
              <g
                key={node.id}
                ref={(el) => {
                  nodeRefs.current[index] = el;
                }}
                role="button"
                tabIndex={0}
                aria-pressed={isActive}
                aria-label={`${KIND_LABEL[node.kind]}: ${node.label}. ${node.note}`}
                className="cursor-pointer outline-none transition-opacity duration-300"
                opacity={dimmed ? 0.18 : 1}
                onMouseEnter={() => setActiveId(node.id)}
                onFocus={() => setActiveId(node.id)}
                onBlur={() => setActiveId(null)}
                onClick={() => setActiveId((cur) => (cur === node.id ? null : node.id))}
                onKeyDown={(event) => onNodeKey(event, node.id)}
              >
                {/* Generous invisible hit target — the glyph itself is small. */}
                <circle cx={node.x} cy={node.y} r={Math.max(r * 2.8, 20)} fill="transparent" />

                <circle
                  cx={node.x}
                  cy={node.y}
                  r={r * (isActive ? 3.2 : 2.7)}
                  fill={color}
                  opacity={isActive ? 0.28 : undefined}
                  className={cn(
                    "origin-center transition-all duration-500",
                    !activeId && "animate-pulse-node",
                  )}
                  style={{ animationDelay: `${(index % 5) * 0.85}s`, transformBox: "fill-box" }}
                />

                {/* Selection ring, drawn only for the active node. */}
                <circle
                  cx={node.x}
                  cy={node.y}
                  r={r * 2.1}
                  fill="none"
                  stroke={color}
                  strokeWidth="1"
                  strokeDasharray="3 5"
                  opacity={isActive ? 0.7 : 0}
                  className="transition-opacity duration-300"
                />

                <NodeGlyph
                  kind={node.kind}
                  x={node.x}
                  y={node.y}
                  r={r}
                  color={color}
                  active={isActive}
                />

                <text
                  x={left ? node.x - r * 2.1 - 5 : node.x + r * 2.1 + 5}
                  y={node.y + 3.5}
                  textAnchor={left ? "end" : "start"}
                  fontSize="10"
                  letterSpacing="0.06em"
                  className={cn(
                    "pointer-events-none font-mono transition-colors duration-300",
                    isActive ? "fill-foreground" : "fill-muted-foreground",
                  )}
                >
                  {node.label}
                </text>
              </g>
            );
          })}
        </g>
      </svg>

      {/* Readout — reserves its own height so selecting a node never reflows. */}
      <p
        aria-live="polite"
        className="min-h-9 border-t border-hairline pt-3 text-[0.8125rem] leading-snug"
      >
        {active ? (
          <>
            <span
              className="font-mono text-[0.6875rem] uppercase tracking-[0.14em]"
              style={{ color: KIND_COLOR[active.kind] }}
            >
              {KIND_LABEL[active.kind]}
            </span>
            <span className="ml-2 font-medium text-foreground">{active.label}</span>
            <span className="ml-2 text-muted-foreground">{active.note}</span>
          </>
        ) : (
          <span className="text-muted-foreground">{hint}</span>
        )}
      </p>
    </div>
  );
}

/**
 * Shape encodes node type, so the graph stays legible for anyone who cannot
 * separate the four hues — colour is reinforcement here, never the only signal.
 */
function NodeGlyph({
  kind,
  x,
  y,
  r,
  color,
  active,
}: {
  kind: NodeKind;
  x: number;
  y: number;
  r: number;
  color: string;
  active: boolean;
}) {
  const stroke = active ? 2.6 : 1.8;
  const shell = { fill: "var(--background)", stroke: color, strokeWidth: stroke };

  if (kind === "project") {
    // Squircle — the heaviest glyph, for the hubs everything hangs off.
    return (
      <g className="transition-all duration-300">
        <rect x={x - r} y={y - r} width={r * 2} height={r * 2} rx={r * 0.42} {...shell} />
        <rect
          x={x - r * 0.38}
          y={y - r * 0.38}
          width={r * 0.76}
          height={r * 0.76}
          rx={r * 0.16}
          fill={color}
        />
      </g>
    );
  }

  if (kind === "decision") {
    // Diamond — a fork in the road.
    return (
      <g className="transition-all duration-300" transform={`rotate(45 ${x} ${y})`}>
        <rect x={x - r * 0.86} y={y - r * 0.86} width={r * 1.72} height={r * 1.72} rx={r * 0.22} {...shell} />
        <rect
          x={x - r * 0.3}
          y={y - r * 0.3}
          width={r * 0.6}
          height={r * 0.6}
          rx={r * 0.1}
          fill={color}
        />
      </g>
    );
  }

  if (kind === "skill") {
    // Open ring — a capability, not an actor.
    return (
      <g className="transition-all duration-300">
        <circle cx={x} cy={y} r={r} fill="var(--background)" stroke={color} strokeWidth={stroke} />
        <circle cx={x} cy={y} r={r * 0.3} fill={color} />
      </g>
    );
  }

  // person — solid disc with a cap ring
  return (
    <g className="transition-all duration-300">
      <circle cx={x} cy={y} r={r} {...shell} />
      <circle cx={x} cy={y} r={r * 0.44} fill={color} />
    </g>
  );
}
