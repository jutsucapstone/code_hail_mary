"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { useReducedMotion } from "framer-motion";

import { cn } from "@/lib/utils";

type NodeKind = "person" | "project" | "decision" | "skill" | "source";

type SourceGlyph = "mail" | "chat" | "code" | "calendar" | "doc";

interface GraphNode {
  id: string;
  label: string;
  kind: NodeKind;
  /** What this node "knows" — surfaced in the readout when it is active. */
  note: string;
  /** Centre in the desktop frame. */
  x: number;
  y: number;
  /** Centre in the narrow frame. Absent means the node is not shown there. */
  mx?: number;
  my?: number;
  /**
   * A person's own hue. Each person's sub-web — their avatar and every edge they
   * anchor — carries it, which is how ownership reads at a glance: three people,
   * three visibly distinct webs over one shared graph.
   */
  accent?: string;
  /** Which mark a source tile carries. */
  glyph?: SourceGlyph;
}

/**
 * Two frames, because one cannot serve both.
 *
 * An SVG scales as a whole, so a label's *rendered* size is its font size times
 * `panelWidth / viewBoxWidth`. The first cut of this used a 780-unit frame inside a
 * panel that measures ~543px on a 1440 desktop and ~288px on a phone — factors of 0.70
 * and 0.37 — which rendered 10.5-unit labels at 7.3px and 3.9px. Perfectly legible in
 * the editor, illegible on the page, and invisible to any check that does not measure
 * the composed result.
 *
 * So the desktop frame is sized to the panel it actually lands in, giving roughly 1:1
 * units-to-pixels, and narrow viewports get their own portrait arrangement with the two
 * least load-bearing chips dropped. A node without `mx`/`my` is absent there.
 */
const DESKTOP_VB = { w: 600, h: 450 };
const MOBILE_VB = { w: 330, h: 480 };

const NODES: GraphNode[] = [
  { id: "p3", label: "Meera", kind: "person", accent: "var(--graph-violet)", note: "Joined 4 months ago; ramped on Atlas.", x: 78, y: 74, mx: 58, my: 56 },
  { id: "p1", label: "Priya", kind: "person", accent: "var(--brand)", note: "Sole owner on 3 critical projects — bus factor 1.", x: 56, y: 214, mx: 50, my: 218 },
  { id: "p2", label: "Arjun", kind: "person", accent: "var(--graph-amber)", note: "Kubernetes cost work across Falcon and Atlas.", x: 84, y: 356, mx: 58, my: 400 },
  { id: "pr1", label: "Falcon", kind: "project", note: "14 decisions, 3 owners, 22 linked meetings.", x: 236, y: 196, mx: 206, my: 130 },
  { id: "pr2", label: "Atlas", kind: "project", note: "Depends on the Falcon storage decision.", x: 432, y: 74, mx: 232, my: 302 },
  { id: "d1", label: "Postgres over Mongo", kind: "decision", note: "Decided 12 Mar — owner Priya, cited to 2 meetings.", x: 420, y: 300, mx: 186, my: 452 },
  { id: "d2", label: "Ship read-only", kind: "decision", note: "Decided 4 Apr — supersedes the write-back plan.", x: 468, y: 186, mx: 206, my: 376 },
  { id: "s1", label: "Kubernetes", kind: "skill", note: "2 contributors ranked by real contributions.", x: 170, y: 290, mx: 186, my: 216 },
  { id: "s3", label: "pgvector", kind: "skill", note: "Introduced by the Postgres decision.", x: 276, y: 384 },
  { id: "s2", label: "GraphRAG", kind: "skill", note: "Emerged from the retrieval decisions on Atlas.", x: 486, y: 392 },
  // Source tiles at the rim: where memory arrives from. Every connector in the
  // product is read-only, and the note says so — the illustration keeps the claim.
  { id: "src1", label: "Mail", kind: "source", glyph: "mail", note: "Read-only connector — threads become cited memory.", x: 30, y: 128, mx: 36, my: 130 },
  { id: "src2", label: "Chat", kind: "source", glyph: "chat", note: "Channel history, under each member's own access.", x: 196, y: 32, mx: 150, my: 34 },
  { id: "src3", label: "Code", kind: "source", glyph: "code", note: "Issues and reviews, linked to the people who wrote them.", x: 34, y: 428 },
  { id: "src4", label: "Calendar", kind: "source", glyph: "calendar", note: "Meetings anchor decisions to the moment they happened.", x: 330, y: 128 },
  { id: "src5", label: "Docs", kind: "source", glyph: "doc", note: "Pages and files, each fact pointing at its span.", x: 546, y: 40, mx: 298, my: 250 },
];

/**
 * `bow` bends each edge; alternating signs keep the web organic, not radial.
 *
 * `soft` marks a derived relationship — drawn dashed. The distinction is the product's,
 * not decoration: "Priya owns Falcon" is asserted by a system of record, while "Falcon
 * led to pgvector" is inferred from a decision. Dashing the second is the graph being
 * honest about which of its own edges were extracted rather than imported.
 */
const EDGES: Array<{ a: string; b: string; bow: number; soft?: boolean }> = [
  { a: "p1", b: "pr1", bow: 0.11 },
  { a: "p1", b: "s1", bow: -0.09, soft: true },
  { a: "p2", b: "s1", bow: 0.1, soft: true },
  { a: "p2", b: "pr1", bow: -0.08 },
  { a: "p3", b: "pr2", bow: 0.09 },
  { a: "p3", b: "pr1", bow: -0.1 },
  { a: "pr1", b: "pr2", bow: 0.09 },
  { a: "pr1", b: "d1", bow: -0.1 },
  { a: "pr2", b: "d2", bow: 0.12 },
  { a: "d1", b: "d2", bow: -0.1 },
  { a: "d1", b: "s3", bow: 0.11, soft: true },
  { a: "d2", b: "s2", bow: -0.12, soft: true },
  { a: "s3", b: "pr1", bow: 0.1, soft: true },
  { a: "s2", b: "d1", bow: 0.11, soft: true },
  // Ingestion edges: a source feeds the graph through a person's own grant, so
  // each tile hangs off the person (or project) whose access it flows through.
  { a: "src1", b: "p1", bow: 0.1, soft: true },
  { a: "src2", b: "p3", bow: -0.09, soft: true },
  { a: "src3", b: "p2", bow: 0.09, soft: true },
  { a: "src4", b: "pr1", bow: -0.08, soft: true },
  { a: "src5", b: "pr2", bow: 0.1, soft: true },
];

/** Edges that carry a travelling packet — the live query path. */
const FLOWS = [
  { edge: 0, offset: 0 },
  { edge: 7, offset: 0.33 },
  { edge: 9, offset: 0.66 },
  { edge: 8, offset: 0.15 },
  { edge: 11, offset: 0.5 },
  // Ingestion streaming in from two of the rim tiles.
  { edge: 14, offset: 0.42 },
  { edge: 18, offset: 0.8 },
];

const KIND_COLOR: Record<NodeKind, string> = {
  person: "var(--brand)",
  project: "var(--foreground)",
  decision: "var(--graph)",
  skill: "var(--graph-muted)",
  source: "var(--muted-foreground)",
};

/** A person's own accent wins over the kind colour everywhere one is set. */
const colorOf = (node: GraphNode) => node.accent ?? KIND_COLOR[node.kind];

const KIND_LABEL: Record<NodeKind, string> = {
  person: "Person",
  project: "Project",
  decision: "Decision",
  skill: "Skill",
  source: "Source",
};

/** Capsule height per kind. Size is one of the three non-colour signals. */
const PILL_H: Record<"project" | "decision" | "skill", number> = {
  project: 32,
  decision: 28,
  skill: 25,
};

/** Half-extent of a source tile — a rounded app square, label in the readout only. */
const TILE_HALF = 15;

const AVATAR_R = 19;

/**
 * Advance width of the mono face at 1px.
 *
 * Deliberately a constant rather than a `getComputedTextLength` call: measuring would
 * make capsule width depend on whether the webfont had loaded, so the server and the
 * first client paint would disagree and React would report a hydration mismatch. The
 * cost of the constant is that a value even slightly low clips the longest label, so it
 * is set from the widest glyph rather than the average.
 */
const MONO_ADVANCE = 0.605;

const fontSizeFor = (kind: NodeKind) => (kind === "project" ? 12 : kind === "decision" ? 11 : 10);

/**
 * The colour an edge carries. A person's edges wear that person's accent — the
 * reference reading of "whose web is this" — a source edge stays neutral (it is
 * plumbing, not ownership), and an edge between entities keeps the brand→graph
 * gradient the rest of the site uses for connection.
 */
function edgeStroke(a: GraphNode, b: GraphNode): string {
  if (a.kind === "source" || b.kind === "source") return "var(--muted-foreground)";
  const person = a.accent ?? b.accent;
  return person ?? "url(#jutsu-graph-edge)";
}

/** Half-width and half-height of a node's painted box, for layout and hit areas. */
function extentOf(node: GraphNode) {
  if (node.kind === "person") return { hw: AVATAR_R, hh: AVATAR_R };
  if (node.kind === "source") return { hw: TILE_HALF, hh: TILE_HALF };
  const h = PILL_H[node.kind];
  const text = node.label.length * fontSizeFor(node.kind) * MONO_ADVANCE;
  // leading glyph square + gap + text + symmetric padding
  const w = h * 0.62 + 8 + text + 26;
  return { hw: w / 2, hh: h / 2 };
}

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
 * Initials for the monogram. Deliberately not a photograph: a landing page showing
 * faces is either using stock models as though they were customers, or real people who
 * did not agree to appear on it. A monogram claims neither.
 */
const initialsOf = (label: string) =>
  label
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");

/**
 * Subscribes to the narrow-viewport media query.
 *
 * The server snapshot is `false` — the desktop frame — so the prerendered HTML carries
 * the desktop composition and a phone re-lays out once on hydration. Guessing narrow on
 * the server instead would ship the wrong markup to every crawler and to every desktop
 * visitor for one frame.
 */
const NARROW_QUERY = "(max-width: 639px)";

/**
 * One `MediaQueryList` for the module, created on first use.
 *
 * Not at module scope: this file is imported during SSR, where `window` does not
 * exist. Not inside the hook either — `useSyncExternalStore` calls `getSnapshot` on
 * every render and after every store change, and a fresh `matchMedia` per call would
 * allocate a listener target each time purely to read one boolean off it.
 */
let narrowMq: MediaQueryList | null = null;
const getNarrowMq = () => (narrowMq ??= window.matchMedia(NARROW_QUERY));

/** Hoisted so their identity is stable; a new `subscribe` each render would make
 *  React tear down and re-add the listener after every single commit. */
const subscribeNarrow = (notify: () => void) => {
  const mq = getNarrowMq();
  mq.addEventListener("change", notify);
  return () => mq.removeEventListener("change", notify);
};
const getNarrowSnapshot = () => getNarrowMq().matches;
const getNarrowServerSnapshot = () => false;

function useNarrowFrame() {
  return useSyncExternalStore(subscribeNarrow, getNarrowSnapshot, getNarrowServerSnapshot);
}

/**
 * The memory graph, drawn as a living system rather than a diagram.
 *
 * Every node carries its own label inside a bordered chip — a monogram disc for a
 * person, a capsule for a project or decision, a lighter capsule for a skill — because
 * a constellation of dots with text floating alongside reads as a sketch, and this is
 * the first thing a visitor sees. Type is signalled three ways over: the shape, the
 * size, and the glyph in the leading square. Colour is reinforcement and never the only
 * channel, so the graph survives a reader who cannot separate green from teal.
 *
 * Beyond the nodes, richness comes from three things working off one set of
 * coordinates:
 *  - edges are quadratic curves, bowed off-axis, which reads as a web rather than a
 *    wheel of straight spokes, and dashed where the relationship was inferred;
 *  - packets travel those curves, showing retrieval moving through the graph instead
 *    of a static "query path" caption;
 *  - a slow drift keeps everything breathing.
 *
 * All of it is driven from a single rAF loop writing SVG attributes directly: ten nodes
 * at 60fps of React re-renders would be waste, and edges, packets and labels must move
 * in the *same* frame as their nodes or the graph tears. The loop suspends off-screen
 * and never starts under `prefers-reduced-motion`. Drift phases derive from the node
 * index — no `Math.random`, so SSR matches.
 */
export function MemoryGraph({ className, hint }: { className?: string; hint?: string }) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const shouldReduceMotion = useReducedMotion();
  const narrow = useNarrowFrame();

  const svgRef = useRef<SVGSVGElement | null>(null);
  const nodeRefs = useRef<Array<SVGGElement | null>>([]);
  const edgeRefs = useRef<Array<SVGPathElement | null>>([]);
  const glowRefs = useRef<Array<SVGPathElement | null>>([]);
  const packetRefs = useRef<Array<SVGCircleElement | null>>([]);

  /**
   * The frame in play and everything derived from it, in one memo.
   *
   * The rAF loop indexes `nodeRefs` and `edgeRefs` by render order, so if the loop and
   * the JSX ever disagreed about which array they were walking, edges would animate
   * against the wrong endpoints — a tear that appears at one breakpoint only, which is
   * exactly the sort of thing that ships.
   */
  const frame = useMemo(() => {
    const vb = narrow ? MOBILE_VB : DESKTOP_VB;
    const nodes = narrow ? NODES.filter((n) => n.mx !== undefined) : NODES;
    const shown = new Set(nodes.map((n) => n.id));
    const at = (n: GraphNode) =>
      narrow ? { x: n.mx as number, y: n.my as number } : { x: n.x, y: n.y };

    const edges = EDGES.filter((e) => shown.has(e.a) && shown.has(e.b));
    // FLOWS index the full edge table; re-point them at the filtered one and drop any
    // whose edge is not on screen.
    const flows = FLOWS.map((f) => ({ ...f, edge: edges.indexOf(EDGES[f.edge]) })).filter(
      (f) => f.edge >= 0,
    );
    const index = new Map(nodes.map((n, i) => [n.id, i]));
    return { vb, nodes, edges, flows, at, index };
  }, [narrow]);

  const neighbours = useMemo(() => {
    if (!activeId) return null;
    const set = new Set<string>([activeId]);
    for (const e of frame.edges) {
      if (e.a === activeId) set.add(e.b);
      if (e.b === activeId) set.add(e.a);
    }
    return set;
  }, [activeId, frame]);

  const active = activeId ? byId(activeId) : null;
  const isDimmed = (id: string) => neighbours !== null && !neighbours.has(id);
  const isEdgeActive = (a: string, b: string) =>
    Boolean(activeId) && (a === activeId || b === activeId);

  useEffect(() => {
    if (shouldReduceMotion) return;
    const svg = svgRef.current;
    if (!svg) return;

    const { nodes, edges, flows, at, index } = frame;
    const refs = nodeRefs.current;

    let raf = 0;
    let running = false;

    // Gentler than the dot-and-label version this replaced. A 6px disc could swim 5px
    // and read as alive; a 180px capsule doing the same reads as unstable, and two of
    // them drifting out of phase look like a layout bug.
    const drift = nodes.map((_, i) => ({
      ax: 2.4 + (i % 3) * 1,
      ay: 2 + ((i + 1) % 4) * 0.8,
      sx: 0.00019 + (i % 5) * 0.00003,
      sy: 0.00024 + (i % 4) * 0.00004,
      px: i * 1.7,
      py: i * 2.3,
    }));
    const home = nodes.map(at);
    const pos = home.map((h) => ({ ...h }));

    const tick = (t: number) => {
      for (let i = 0; i < nodes.length; i++) {
        const d = drift[i];
        pos[i].x = home[i].x + Math.sin(t * d.sx + d.px) * d.ax;
        pos[i].y = home[i].y + Math.cos(t * d.sy + d.py) * d.ay;
        nodeRefs.current[i]?.setAttribute(
          "transform",
          `translate(${(pos[i].x - home[i].x).toFixed(2)} ${(pos[i].y - home[i].y).toFixed(2)})`,
        );
      }

      for (let i = 0; i < edges.length; i++) {
        const e = edges[i];
        const a = pos[index.get(e.a)!];
        const b = pos[index.get(e.b)!];
        const { cx, cy } = control(a.x, a.y, b.x, b.y, e.bow);
        const d = `M${a.x.toFixed(1)} ${a.y.toFixed(1)}Q${cx.toFixed(1)} ${cy.toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
        edgeRefs.current[i]?.setAttribute("d", d);
        glowRefs.current[i]?.setAttribute("d", d);
      }

      for (let i = 0; i < flows.length; i++) {
        const dot = packetRefs.current[i];
        if (!dot) continue;
        const e = edges[flows[i].edge];
        const a = pos[index.get(e.a)!];
        const b = pos[index.get(e.b)!];
        const { cx, cy } = control(a.x, a.y, b.x, b.y, e.bow);
        const p = (((t / 3600 + flows[i].offset) % 1) + 1) % 1;
        // Ease the packet so it slows into the target node, then restarts.
        const eased = p < 0.85 ? p / 0.85 : 1;
        dot.setAttribute("cx", quad(a.x, cx, b.x, eased).toFixed(1));
        dot.setAttribute("cy", quad(a.y, cy, b.y, eased).toFixed(1));
        dot.setAttribute("opacity", String(p < 0.85 ? Math.sin((p / 0.85) * Math.PI) * 0.9 : 0));
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
      // The frame is about to change, and React reuses these elements for whichever
      // node lands at the same index next. A leftover translate would offset that
      // node permanently, since the new loop only ever writes deltas from its own home.
      for (const g of refs) g?.removeAttribute("transform");
    };
  }, [shouldReduceMotion, frame]);

  const onNodeKey = useCallback((event: React.KeyboardEvent, id: string) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setActiveId((cur) => (cur === id ? null : id));
    }
    if (event.key === "Escape") setActiveId(null);
  }, []);

  const staticPath = (e: (typeof EDGES)[number]) => {
    const a = frame.at(byId(e.a));
    const b = frame.at(byId(e.b));
    const { cx, cy } = control(a.x, a.y, b.x, b.y, e.bow);
    return `M${a.x} ${a.y}Q${cx} ${cy} ${b.x} ${b.y}`;
  };

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${frame.vb.w} ${frame.vb.h}`}
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
          {/* The survey-paper lattice under everything: structure without noise. */}
          <pattern id="jutsu-graph-lattice" width="26" height="26" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="1" fill="var(--foreground)" opacity="0.07" />
          </pattern>
          <linearGradient id="jutsu-graph-edge" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--brand)" stopOpacity="0.62" />
            <stop offset="100%" stopColor="var(--graph)" stopOpacity="0.62" />
          </linearGradient>
          <filter id="jutsu-graph-bloom" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="4" />
          </filter>
          {/* Lifts a chip off the web behind it. Kept very soft — a hard shadow on a
              flat design system reads as a different product. */}
          <filter id="jutsu-graph-lift" x="-40%" y="-40%" width="180%" height="180%">
            <feDropShadow
              dx="0"
              dy="2"
              stdDeviation="3"
              floodColor="var(--foreground)"
              floodOpacity="0.07"
            />
          </filter>
        </defs>

        <rect
          x="0"
          y="0"
          width={frame.vb.w}
          height={frame.vb.h}
          fill="url(#jutsu-graph-lattice)"
          opacity="0.5"
        />
        <rect x="0" y="0" width={frame.vb.w} height={frame.vb.h} fill="url(#jutsu-graph-halo)" />

        {/* Bloom pass: a blurred copy under the crisp edges, for depth. */}
        <g fill="none" filter="url(#jutsu-graph-bloom)">
          {frame.edges.map((e, i) => {
            const accent = byId(e.a).accent ?? byId(e.b).accent ?? "var(--brand)";
            return (
              <path
                key={`glow-${e.a}-${e.b}`}
                ref={(el) => {
                  glowRefs.current[i] = el;
                }}
                d={staticPath(e)}
                stroke={accent}
                strokeWidth={isEdgeActive(e.a, e.b) ? 3 : 0}
                strokeOpacity={isEdgeActive(e.a, e.b) ? 0.5 : 0}
                className="transition-[stroke-opacity,stroke-width] duration-300"
              />
            );
          })}
        </g>

        <g fill="none">
          {frame.edges.map((e, i) => {
            const lit = isEdgeActive(e.a, e.b);
            const a = byId(e.a);
            const b = byId(e.b);
            const rest = edgeStroke(a, b);
            const litStroke = a.accent ?? b.accent ?? "var(--brand)";
            const isSource = a.kind === "source" || b.kind === "source";
            return (
              <path
                key={`${e.a}-${e.b}`}
                ref={(el) => {
                  edgeRefs.current[i] = el;
                }}
                d={staticPath(e)}
                stroke={lit ? litStroke : rest}
                strokeWidth={lit ? 2.4 : 1.4}
                strokeOpacity={activeId ? (lit ? 1 : 0.07) : isSource ? 0.28 : 0.45}
                strokeDasharray={e.soft ? "5 6" : undefined}
                strokeLinecap="round"
                className="transition-[stroke-opacity,stroke-width] duration-300"
              />
            );
          })}
        </g>

        {/* Packets: retrieval moving through the graph, not a static caption. */}
        <g className={cn("transition-opacity duration-300", activeId && "opacity-0")}>
          {frame.flows.map((f, i) => (
            <circle
              key={`packet-${f.edge}-${f.offset}`}
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
          {frame.nodes.map((node, index) => {
            const dimmed = isDimmed(node.id);
            const isActive = node.id === activeId;
            const { x, y } = frame.at(node);
            const { hw, hh } = extentOf(node);

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
                opacity={dimmed ? 0.16 : 1}
                onMouseEnter={() => setActiveId(node.id)}
                onFocus={() => setActiveId(node.id)}
                onBlur={() => setActiveId(null)}
                onClick={() => setActiveId((cur) => (cur === node.id ? null : node.id))}
                onKeyDown={(event) => onNodeKey(event, node.id)}
              >
                {/* Hit target tracks the painted box, padded. The chip is the affordance
                    now, so a hit area much larger than it would steal hovers from
                    whatever sits alongside. */}
                <rect
                  x={x - hw - 8}
                  y={y - hh - 8}
                  width={hw * 2 + 16}
                  height={hh * 2 + 16}
                  rx={hh + 8}
                  fill="transparent"
                />

                {/* Focus ring, drawn for keyboard and pointer alike. */}
                <rect
                  x={x - hw - 5}
                  y={y - hh - 5}
                  width={hw * 2 + 10}
                  height={hh * 2 + 10}
                  rx={hh + 5}
                  fill="none"
                  stroke={colorOf(node)}
                  strokeWidth="1"
                  strokeDasharray="3 5"
                  opacity={isActive ? 0.65 : 0}
                  className="transition-opacity duration-300"
                />

                <NodeChip node={node} x={x} y={y} active={isActive} />
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
              style={{ color: colorOf(active) }}
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
 * One node, drawn as a labelled chip.
 *
 * Three signals separate the kinds and none of them is colour: a person is a circle
 * where everything else is a capsule; the capsules step down in height and border
 * weight from project to decision to skill; and each carries a distinct glyph in its
 * leading square. A skill's border is dashed as a fourth cue, which also matches the
 * dashed edges it tends to sit on — inferred, not imported.
 */
function NodeChip({
  node,
  x,
  y,
  active,
}: {
  node: GraphNode;
  x: number;
  y: number;
  active: boolean;
}) {
  const color = colorOf(node);

  if (node.kind === "person") {
    // A filled disc in the person's own hue — the anchor weight of the reference
    // treatments — with the monogram knocked out in the page background. Deliberately
    // still not a photograph: stock faces posing as customers claim something false,
    // and real ones did not agree to a landing page.
    return (
      <g className="transition-all duration-300" filter="url(#jutsu-graph-lift)">
        {/* Halo ring in the page ground lifts the disc off edges passing under it. */}
        <circle cx={x} cy={y} r={AVATAR_R + 3} fill="var(--background)" opacity="0.9" />
        <circle
          cx={x}
          cy={y}
          r={AVATAR_R}
          fill={color}
          stroke="var(--background)"
          strokeWidth="1.5"
        />
        <circle
          cx={x}
          cy={y}
          r={AVATAR_R}
          fill="none"
          stroke={color}
          strokeWidth={active ? 2.2 : 1}
          opacity={active ? 0.55 : 0.35}
          style={{
            transformOrigin: `${x}px ${y}px`,
            transform: active ? "scale(1.22)" : "scale(1.12)",
          }}
          className="transition-all duration-300"
        />
        <text
          x={x}
          y={y}
          textAnchor="middle"
          dominantBaseline="central"
          fontSize="13"
          fontWeight="650"
          letterSpacing="0.05em"
          fill="var(--background)"
          className="pointer-events-none select-none font-mono"
        >
          {initialsOf(node.label)}
        </text>
      </g>
    );
  }

  if (node.kind === "source") {
    // An app tile at the rim: rounded square, glyph only. The label lives in the
    // readout and the accessible name — at 30px a caption would just be noise.
    const r = TILE_HALF;
    return (
      <g className="transition-all duration-300" filter="url(#jutsu-graph-lift)">
        <rect
          x={x - r}
          y={y - r}
          width={r * 2}
          height={r * 2}
          rx={9}
          fill="var(--surface-raised)"
          stroke={active ? "var(--foreground)" : "var(--hairline-strong)"}
          strokeWidth={active ? 1.6 : 1}
          className="transition-all duration-300"
        />
        <SourceGlyphMark
          glyph={node.glyph ?? "doc"}
          cx={x}
          cy={y}
          size={13}
          color={active ? "var(--foreground)" : "var(--muted-foreground)"}
        />
      </g>
    );
  }

  const h = PILL_H[node.kind];
  const { hw } = extentOf(node);
  const fs = fontSizeFor(node.kind);
  const chip = h * 0.62;
  const left = x - hw;
  const dashed = node.kind === "skill";

  return (
    <g className="transition-all duration-300" filter="url(#jutsu-graph-lift)">
      <rect
        x={left}
        y={y - h / 2}
        width={hw * 2}
        height={h}
        rx={h / 2}
        fill="var(--surface-raised)"
        stroke={color}
        strokeWidth={active ? 2.2 : node.kind === "project" ? 1.7 : 1.2}
        strokeDasharray={dashed && !active ? "4 4" : undefined}
        strokeOpacity={node.kind === "skill" ? 0.75 : 1}
      />

      {/* Leading glyph square — the app-tile idea, carrying the kind. */}
      <rect
        x={left + 9}
        y={y - chip / 2}
        width={chip}
        height={chip}
        rx={chip * 0.3}
        fill={color}
        opacity={node.kind === "project" ? 1 : 0.16}
      />
      <KindGlyph
        kind={node.kind}
        cx={left + 9 + chip / 2}
        cy={y}
        size={chip * 0.52}
        color={node.kind === "project" ? "var(--background)" : color}
      />

      <text
        x={left + 9 + chip + 8}
        y={y}
        dominantBaseline="central"
        fontSize={fs}
        letterSpacing="0.04em"
        className={cn(
          "pointer-events-none select-none font-mono transition-colors duration-300",
          active || node.kind === "project" ? "fill-foreground" : "fill-muted-foreground",
        )}
      >
        {node.label}
      </text>
    </g>
  );
}

/** Small mark inside a chip's leading square. Stroked so it stays legible at 10px. */
function KindGlyph({
  kind,
  cx,
  cy,
  size,
  color,
}: {
  kind: NodeKind;
  cx: number;
  cy: number;
  size: number;
  color: string;
}) {
  const s = size / 2;
  const common = {
    stroke: color,
    strokeWidth: 1.5,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    fill: "none",
  };

  if (kind === "project") {
    // A facet — the hub everything else hangs off.
    return (
      <g {...common} className="pointer-events-none">
        <path d={`M${cx - s} ${cy} ${cx} ${cy - s} ${cx + s} ${cy} ${cx} ${cy + s} Z`} />
      </g>
    );
  }

  if (kind === "decision") {
    // A fork: one path in, two out.
    return (
      <g {...common} className="pointer-events-none">
        <path d={`M${cx - s} ${cy + s} L${cx} ${cy} L${cx + s} ${cy - s}`} />
        <path d={`M${cx} ${cy} L${cx + s} ${cy + s}`} />
      </g>
    );
  }

  // skill — a spark
  return (
    <g {...common} className="pointer-events-none">
      <path d={`M${cx} ${cy - s} L${cx} ${cy + s}`} />
      <path d={`M${cx - s} ${cy} L${cx + s} ${cy}`} />
    </g>
  );
}

/** The five source marks, stroked to match the kind glyphs' weight. */
function SourceGlyphMark({
  glyph,
  cx,
  cy,
  size,
  color,
}: {
  glyph: SourceGlyph;
  cx: number;
  cy: number;
  size: number;
  color: string;
}) {
  const s = size / 2;
  const common = {
    stroke: color,
    strokeWidth: 1.5,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    fill: "none",
    className: "pointer-events-none transition-colors duration-300",
  };

  if (glyph === "mail") {
    return (
      <g {...common}>
        <rect x={cx - s} y={cy - s * 0.72} width={s * 2} height={s * 1.44} rx={1.5} />
        <path d={`M${cx - s} ${cy - s * 0.5} L${cx} ${cy + s * 0.16} L${cx + s} ${cy - s * 0.5}`} />
      </g>
    );
  }

  if (glyph === "chat") {
    return (
      <g {...common}>
        <path
          d={`M${cx - s} ${cy - s * 0.7} h${s * 2} a1.5 1.5 0 0 1 1.5 1.5 v${s * 0.9} a1.5 1.5 0 0 1 -1.5 1.5 h${-s * 1.1} l${-s * 0.55} ${s * 0.62} v-${s * 0.62} h${-s * 0.35} a1.5 1.5 0 0 1 -1.5 -1.5 v-${s * 0.9} a1.5 1.5 0 0 1 1.5 -1.5 Z`}
        />
      </g>
    );
  }

  if (glyph === "code") {
    return (
      <g {...common}>
        <path d={`M${cx - s * 0.35} ${cy - s} L${cx - s} ${cy} L${cx - s * 0.35} ${cy + s}`} />
        <path d={`M${cx + s * 0.35} ${cy - s} L${cx + s} ${cy} L${cx + s * 0.35} ${cy + s}`} />
      </g>
    );
  }

  if (glyph === "calendar") {
    return (
      <g {...common}>
        <rect x={cx - s} y={cy - s * 0.78} width={s * 2} height={s * 1.68} rx={1.5} />
        <path d={`M${cx - s} ${cy - s * 0.28} H${cx + s}`} />
        <path d={`M${cx - s * 0.45} ${cy - s * 1.05} V${cy - s * 0.55}`} />
        <path d={`M${cx + s * 0.45} ${cy - s * 1.05} V${cy - s * 0.55}`} />
      </g>
    );
  }

  // doc — a page with a folded corner
  return (
    <g {...common}>
      <path
        d={`M${cx - s * 0.7} ${cy - s} H${cx + s * 0.2} L${cx + s * 0.7} ${cy - s * 0.5} V${cy + s} H${cx - s * 0.7} Z`}
      />
      <path d={`M${cx + s * 0.2} ${cy - s} V${cy - s * 0.5} H${cx + s * 0.7}`} />
    </g>
  );
}
