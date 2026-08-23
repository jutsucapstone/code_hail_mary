/**
 * Press feedback: a synthesised click, and a haptic tick where the hardware exists.
 *
 * Framework-free on purpose, and delegated from the document rather than wired into
 * components. A press costs no React work at all — no provider re-render, no `onClick`
 * threaded through the several dozen places that render a control, and nothing to
 * remember when the next one is added. It also survives `shadcn` regenerating
 * `components/ui/button.tsx`, which editing that file would not.
 *
 * **Sound is off until asked for.** A site that makes noise at someone who did not ask
 * is the single most-resented thing a page can do, and the fact that we can do it well
 * is not a reason to do it uninvited. Haptics default on: silent, gesture-gated, absent
 * entirely on hardware that cannot buzz, and conventional on the phones that can.
 *
 * Nothing here runs during render or on the server — every entry point is called from a
 * `useEffect` or a real user gesture.
 */

/** The two channels, preferred independently. */
export type FeedbackChannel = "sound" | "haptics";

/**
 * Namespaced to match `jutsu:announcement:*`, the other persisted preference on this
 * origin. `localStorage` is shared per-origin, so an unprefixed key is a collision
 * waiting for the next thing deployed here.
 */
const STORAGE_KEY: Record<FeedbackChannel, string> = {
  sound: "jutsu:feedback:sound",
  haptics: "jutsu:feedback:haptics",
};

const DEFAULT: Record<FeedbackChannel, boolean> = {
  sound: false,
  haptics: true,
};

/** Same-tab notification. `storage` only fires in *other* tabs, so it cannot carry this. */
const CHANGE_EVENT = "jutsu:feedback-change";

/* -------------------------------------------------------------------------- */
/* Preferences                                                                 */
/* -------------------------------------------------------------------------- */

export function readPreference(channel: FeedbackChannel): boolean {
  try {
    const stored = localStorage.getItem(STORAGE_KEY[channel]);
    if (stored === "1") return true;
    if (stored === "0") return false;
  } catch {
    // Private mode or blocked storage. Fall through to the default rather than throw
    // out of something as inconsequential as whether a button ticks.
  }
  return DEFAULT[channel];
}

export function setPreference(channel: FeedbackChannel, enabled: boolean): void {
  try {
    localStorage.setItem(STORAGE_KEY[channel], enabled ? "1" : "0");
  } catch {
    // Not persisting is survivable; the in-memory value below still takes effect for
    // this page view, so the control the visitor just pressed still does what it says.
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function subscribePreferences(onChange: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

/**
 * Whether this device can actually vibrate.
 *
 * iOS Safari does not implement the Vibration API at all and there is no web-exposed
 * substitute, so on iPhone this is honestly `false` rather than quietly pretending.
 * Used to decide whether to *show* the haptics control — offering a switch that cannot
 * do anything is worse than not offering one.
 */
export function supportsHaptics(): boolean {
  return typeof navigator !== "undefined" && typeof navigator.vibrate === "function";
}

/** A vibration is movement, and someone asking for less of it means this too. */
function motionIsWelcome(): boolean {
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/* -------------------------------------------------------------------------- */
/* Voices                                                                      */
/* -------------------------------------------------------------------------- */

interface Tone {
  /** Peak gain of the body. Low by design — this is furniture, not an alert. */
  readonly gain: number;
  readonly fromHz: number;
  readonly toHz: number;
  /** Body decay, seconds. */
  readonly decay: number;
  /** Peak gain of the noise transient that gives the click its edge. */
  readonly snap: number;
  /** Vibration length, milliseconds. */
  readonly haptic: number;
}

/**
 * Two voices, because a primary action and a tertiary icon button carrying identical
 * weight is what makes UI sound feel bolted on. A pitch drop reads as "pressed" the way
 * a real switch does; a flat blip reads as a notification, which is the wrong verb.
 */
const PRIMARY: Tone = {
  gain: 0.085,
  fromHz: 660,
  toHz: 300,
  decay: 0.07,
  snap: 0.03,
  haptic: 12,
};

const SECONDARY: Tone = {
  gain: 0.05,
  fromHz: 920,
  toHz: 520,
  decay: 0.045,
  snap: 0.018,
  haptic: 8,
};

/** Long enough not to click, short enough to read as instant. */
const ATTACK = 0.0015;
const SNAP_DECAY = 0.012;
/** `exponentialRampToValueAtTime` is undefined at zero, so silence is asymptotic. */
const SILENCE = 0.0001;

let context: AudioContext | null = null;
let master: GainNode | null = null;
let noise: AudioBuffer | null = null;

/**
 * Built on first press, never on load.
 *
 * Constructing an AudioContext eagerly leaves a suspended context on every page view —
 * which browsers may surface as a tab "playing media" indicator, and which costs an
 * audio thread for visitors who never turn sound on. Called only from a gesture handler,
 * so autoplay policy is satisfied by construction.
 */
function ensureContext(): AudioContext | null {
  if (context) return context;

  const Ctor =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext;
  if (!Ctor) return null;

  context = new Ctor();
  master = context.createGain();
  master.gain.value = 1;
  master.connect(context.destination);
  return context;
}

/** One buffer of white noise, reused by every click. ~50ms is far more than we sample. */
function noiseBuffer(ctx: AudioContext): AudioBuffer {
  if (noise) return noise;
  const frames = Math.floor(ctx.sampleRate * 0.05);
  const buffer = ctx.createBuffer(1, frames, ctx.sampleRate);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < frames; i += 1) {
    channel[i] = Math.random() * 2 - 1;
  }
  noise = buffer;
  return buffer;
}

function playTone(tone: Tone): void {
  const ctx = ensureContext();
  if (!ctx || !master) return;

  // A context can be interrupted — a phone call on iOS, another tab taking the device.
  // Resuming is a no-op when it is already running.
  if (ctx.state !== "running") void ctx.resume();

  const t = ctx.currentTime;

  // Body: a short pitch drop. This is the part you hear as the click's weight.
  const body = ctx.createOscillator();
  body.type = "triangle";
  body.frequency.setValueAtTime(tone.fromHz, t);
  body.frequency.exponentialRampToValueAtTime(tone.toHz, t + tone.decay);

  const bodyGain = ctx.createGain();
  bodyGain.gain.setValueAtTime(SILENCE, t);
  bodyGain.gain.exponentialRampToValueAtTime(tone.gain, t + ATTACK);
  bodyGain.gain.exponentialRampToValueAtTime(SILENCE, t + tone.decay);

  body.connect(bodyGain).connect(master);
  body.start(t);
  // Stopping releases the node for collection; without it every press leaks a voice.
  body.stop(t + tone.decay + 0.02);

  // Snap: a high, very short noise transient. Without it the click sounds synthetic —
  // this is the edge that a real switch has and a pure oscillator does not.
  const snap = ctx.createBufferSource();
  snap.buffer = noiseBuffer(ctx);

  const edge = ctx.createBiquadFilter();
  edge.type = "highpass";
  edge.frequency.value = 2400;

  const snapGain = ctx.createGain();
  snapGain.gain.setValueAtTime(tone.snap, t);
  snapGain.gain.exponentialRampToValueAtTime(SILENCE, t + SNAP_DECAY);

  snap.connect(edge).connect(snapGain).connect(master);
  snap.start(t);
  snap.stop(t + SNAP_DECAY + 0.01);
}

/* -------------------------------------------------------------------------- */
/* Delegation                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * What counts as a control.
 *
 * `[data-slot="button"]` catches every shadcn `Button`, including the `asChild` CTAs
 * that render as anchors. Plain nav and footer links are deliberately excluded: making
 * every link on the page tick is how this stops feeling considered and starts feeling
 * like a toy.
 */
const CONTROL = 'button, [role="button"], [data-slot="button"]';

/** Opt out of feedback for one control with `data-feedback="off"`. */
const OPT_OUT = "off";

/**
 * Below a double-click's cadence, so it only ever suppresses rates no hand produces —
 * a held Enter key repeating, or a script clicking in a loop. Without it those stack
 * voices into a buzz.
 */
const MIN_INTERVAL_MS = 40;

let lastFiredAt = -Infinity;

/**
 * The control the last real pointer press landed on, and when.
 *
 * Used to stop a press being answered twice. The keyboard branch below keys off
 * `detail === 0`, which is the documented way to spot a synthesised click — but touch
 * clicks are not guaranteed to report a non-zero `detail` on every engine, and they
 * arrive a few hundred milliseconds after the `pointerdown` that already fired. So the
 * click branch also declines anything that follows a press on the same element.
 */
let lastPointerControl: Element | null = null;
let lastPointerAt = -Infinity;

/** Comfortably longer than the pointerdown→click gap, far shorter than a deliberate re-press. */
const POINTER_ECHO_MS = 700;

/**
 * `HTMLElement` is not enough: the memory graph's nodes are SVG `<g role="button">`
 * elements, and `Element.closest` walks into them perfectly well. Both interfaces carry
 * `dataset`, which is all this needs.
 */
type Control = HTMLElement | SVGElement;

function controlFrom(target: EventTarget | null): Control | null {
  if (!(target instanceof Element)) return null;

  const found = target.closest(CONTROL);
  if (!(found instanceof HTMLElement || found instanceof SVGElement)) return null;
  const control: Control = found;

  // A control that cannot be used should not answer as though it can. `aria-disabled`
  // matters as much as `disabled` here: Radix uses it for controls that stay focusable.
  if (control.hasAttribute("disabled")) return null;
  if (control.getAttribute("aria-disabled") === "true") return null;
  if (control.dataset.feedback === OPT_OUT) return null;

  return control;
}

function fire(control: Control): void {
  // Nothing from a backgrounded tab. A timer or a message handler firing a synthetic
  // press should not make a sound in a window nobody is looking at.
  if (document.hidden) return;

  const now = performance.now();
  if (now - lastFiredAt < MIN_INTERVAL_MS) return;
  lastFiredAt = now;

  // `data-variant` is stamped by the shadcn Button and survives `asChild`, so the CTA
  // that matters gets the fuller voice without anything being labelled by hand.
  const variant = control.dataset.variant;
  const tone = variant === "default" || variant === "destructive" ? PRIMARY : SECONDARY;

  if (readPreference("sound")) playTone(tone);
  if (readPreference("haptics") && supportsHaptics() && motionIsWelcome()) {
    navigator.vibrate(tone.haptic);
  }
}

function onPointerDown(event: PointerEvent): void {
  // Press, not release — hardware answers under the finger, and matching that is most
  // of why this reads as tactile rather than as a sound effect.
  //
  // It is also the only moment that works for the pilot forms: `SubmitButton` sets
  // `disabled` the instant it is pressed, and the base button class carries
  // `disabled:pointer-events-none`, so the click event for a submit never arrives.
  if (!event.isTrusted || event.button !== 0) return;

  const control = controlFrom(event.target);
  if (!control) return;

  lastPointerControl = control;
  lastPointerAt = performance.now();
  fire(control);
}

function onClick(event: MouseEvent): void {
  // Keyboard activation of a native button, which produces no pointer event at all.
  if (!event.isTrusted || event.detail !== 0) return;

  const control = controlFrom(event.target);
  if (!control) return;

  // …unless a real press just landed here, in which case this is that press's own
  // click arriving late rather than a keyboard one.
  if (
    control === lastPointerControl &&
    performance.now() - lastPointerAt < POINTER_ECHO_MS
  ) {
    return;
  }

  fire(control);
}

/** Enter and Space, the two keys that activate a control. */
const ACTIVATION_KEYS = new Set([" ", "Enter", "Spacebar"]);

function onKeyDown(event: KeyboardEvent): void {
  // Only for elements carrying an explicit `role="button"` — today that is the memory
  // graph's SVG nodes, which handle Enter and Space themselves and set state directly
  // instead of synthesising a click, so the branch above never sees them. Native
  // buttons do synthesise one and are deliberately excluded here, or they would tick
  // twice. `role="radio"` and `role="tab"` are excluded for the same reason.
  if (!event.isTrusted || event.repeat) return;
  if (!ACTIVATION_KEYS.has(event.key)) return;

  const target = event.target;
  if (!(target instanceof Element)) return;
  if (!target.closest('[role="button"]')) return;

  const control = controlFrom(target);
  if (control) fire(control);
}

/**
 * Demonstrate a channel that has just been switched on.
 *
 * Needed because the delegated handler runs on `pointerdown`, which is *before* the
 * toggle's own click flips the preference — so without this, turning sound on is met
 * with silence while turning it off is the one press that makes a noise, which is
 * exactly backwards. Call it after enabling, so the answer to "what does this do" is
 * the thing itself.
 *
 * Still inside the click that enabled it, so autoplay policy is satisfied.
 */
export function preview(channel: FeedbackChannel): void {
  if (channel === "sound") {
    playTone(PRIMARY);
    return;
  }
  if (supportsHaptics() && motionIsWelcome()) navigator.vibrate(PRIMARY.haptic);
}

/**
 * Attach the listeners. Returns a teardown for the effect that called it.
 *
 * Both are passive and neither ever calls `preventDefault`, so this cannot interfere
 * with scrolling, with a control's own handler, or with the order they run in.
 */
export function startFeedback(): () => void {
  document.addEventListener("pointerdown", onPointerDown, { passive: true });
  document.addEventListener("click", onClick, { passive: true });
  document.addEventListener("keydown", onKeyDown, { passive: true });

  return () => {
    document.removeEventListener("pointerdown", onPointerDown);
    document.removeEventListener("click", onClick);
    document.removeEventListener("keydown", onKeyDown);

    // Hand the audio hardware back. Guarded because React runs effects twice in
    // development, and closing a context twice throws.
    if (context && context.state !== "closed") void context.close();
    context = null;
    master = null;
    noise = null;
  };
}
