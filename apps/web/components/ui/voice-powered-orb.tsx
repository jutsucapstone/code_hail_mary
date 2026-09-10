"use client";

import { useEffect, useRef, type FC } from "react";
import { Mesh, Program, Renderer, Triangle, Vec3 } from "ogl";

import { REDUCED_MOTION_QUERY, useMediaQuery } from "@/lib/use-media-query";
import { cn } from "@/lib/utils";

/**
 * A WebGL orb that breathes on its own and moves with the voice in the room.
 *
 * Adapted from a community component. The props and the look are the original's; what
 * changed is everything that decides whether it is safe to leave running on a page
 * people work in:
 *
 * * **One owner for the microphone.** The original opened it from two effects that
 *   raced — the second's clean-up could tear the first's stream down mid-setup and
 *   leave a track live with nothing left to stop it, which reads to a person as the
 *   browser's recording indicator staying on after they pressed stop. Here one effect
 *   keyed on `enableVoiceControl` opens it, and its clean-up — or an open that resolves
 *   after the clean-up — stops every track.
 * * **Off by default.** `enableVoiceControl` used to default to `true`, so mounting the
 *   component was enough to prompt for, and start, a recording.
 * * **The canvas is sized once.** OGL's `setSize` already multiplies by `dpr`; the
 *   original multiplied again, drawing four times the pixels on a 2× screen. The ratio
 *   is also capped at 2.
 * * **It stops when nobody is looking** — off-screen it does not draw, and under
 *   `prefers-reduced-motion` it neither animates nor opens the microphone: the still
 *   below is the whole orb.
 * * **No WebGL, no crash.** OGL dereferences a missing context and throws, so the
 *   context is probed first, and the same still stands in for the canvas until the
 *   first frame is drawn — and for good if it never is.
 * * **Brand colours**, the greens and teal of the design tokens instead of violet and
 *   cyan. `hue` still rotates them.
 * * `onVoiceDetected` fires when its answer changes, not on every animation frame.
 */
export interface VoicePoweredOrbProps {
  className?: string;
  /** Rotates the palette, in degrees. 0 is the brand's greens and teal. */
  hue?: number;
  /** Open the microphone and let the voice level drive the orb. */
  enableVoiceControl?: boolean;
  voiceSensitivity?: number;
  maxRotationSpeed?: number;
  maxHoverIntensity?: number;
  /** Called when the orb starts or stops hearing a voice — on the change only. */
  onVoiceDetected?: (detected: boolean) => void;
}

const VERT = /* glsl */ `
  precision highp float;
  attribute vec2 position;
  attribute vec2 uv;
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = vec4(position, 0.0, 1.0);
  }
`;

// The original's shader; only the three base colours differ. They are the design
// tokens in sRGB — `--brand` (dark theme), `--graph` (dark theme) and `--brand`
// (light theme) from packages/ui/src/tokens.css — so green leads and teal follows,
// as it does everywhere else on the product.
const FRAG = /* glsl */ `
  precision highp float;

  uniform float iTime;
  uniform vec3 iResolution;
  uniform float hue;
  uniform float hover;
  uniform float rot;
  uniform float hoverIntensity;
  varying vec2 vUv;

  vec3 rgb2yiq(vec3 c) {
    float y = dot(c, vec3(0.299, 0.587, 0.114));
    float i = dot(c, vec3(0.596, -0.274, -0.322));
    float q = dot(c, vec3(0.211, -0.523, 0.312));
    return vec3(y, i, q);
  }

  vec3 yiq2rgb(vec3 c) {
    float r = c.x + 0.956 * c.y + 0.621 * c.z;
    float g = c.x - 0.272 * c.y - 0.647 * c.z;
    float b = c.x - 1.106 * c.y + 1.703 * c.z;
    return vec3(r, g, b);
  }

  vec3 adjustHue(vec3 color, float hueDeg) {
    float hueRad = hueDeg * 3.14159265 / 180.0;
    vec3 yiq = rgb2yiq(color);
    float cosA = cos(hueRad);
    float sinA = sin(hueRad);
    float i = yiq.y * cosA - yiq.z * sinA;
    float q = yiq.y * sinA + yiq.z * cosA;
    yiq.y = i;
    yiq.z = q;
    return yiq2rgb(yiq);
  }

  vec3 hash33(vec3 p3) {
    p3 = fract(p3 * vec3(0.1031, 0.11369, 0.13787));
    p3 += dot(p3, p3.yxz + 19.19);
    return -1.0 + 2.0 * fract(vec3(p3.x + p3.y, p3.x + p3.z, p3.y + p3.z) * p3.zyx);
  }

  float snoise3(vec3 p) {
    const float K1 = 0.333333333;
    const float K2 = 0.166666667;
    vec3 i = floor(p + (p.x + p.y + p.z) * K1);
    vec3 d0 = p - (i - (i.x + i.y + i.z) * K2);
    vec3 e = step(vec3(0.0), d0 - d0.yzx);
    vec3 i1 = e * (1.0 - e.zxy);
    vec3 i2 = 1.0 - e.zxy * (1.0 - e);
    vec3 d1 = d0 - (i1 - K2);
    vec3 d2 = d0 - (i2 - K1);
    vec3 d3 = d0 - 0.5;
    vec4 h = max(0.6 - vec4(dot(d0, d0), dot(d1, d1), dot(d2, d2), dot(d3, d3)), 0.0);
    vec4 n = h * h * h * h * vec4(
      dot(d0, hash33(i)),
      dot(d1, hash33(i + i1)),
      dot(d2, hash33(i + i2)),
      dot(d3, hash33(i + 1.0))
    );
    return dot(vec4(31.316), n);
  }

  vec4 extractAlpha(vec3 colorIn) {
    float a = max(max(colorIn.r, colorIn.g), colorIn.b);
    return vec4(colorIn.rgb / (a + 1e-5), a);
  }

  const vec3 baseColor1 = vec3(0.509, 0.787, 0.200);
  const vec3 baseColor2 = vec3(0.312, 0.746, 0.755);
  const vec3 baseColor3 = vec3(0.215, 0.464, 0.062);
  const float innerRadius = 0.6;
  const float noiseScale = 0.65;

  float light1(float intensity, float attenuation, float dist) {
    return intensity / (1.0 + dist * attenuation);
  }

  float light2(float intensity, float attenuation, float dist) {
    return intensity / (1.0 + dist * dist * attenuation);
  }

  vec4 draw(vec2 uv) {
    vec3 color1 = adjustHue(baseColor1, hue);
    vec3 color2 = adjustHue(baseColor2, hue);
    vec3 color3 = adjustHue(baseColor3, hue);

    float ang = atan(uv.y, uv.x);
    float len = length(uv);
    float invLen = len > 0.0 ? 1.0 / len : 0.0;

    float n0 = snoise3(vec3(uv * noiseScale, iTime * 0.5)) * 0.5 + 0.5;
    float r0 = mix(mix(innerRadius, 1.0, 0.4), mix(innerRadius, 1.0, 0.6), n0);
    float d0 = distance(uv, (r0 * invLen) * uv);
    float v0 = light1(1.0, 10.0, d0);
    v0 *= smoothstep(r0 * 1.05, r0, len);
    float cl = cos(ang + iTime * 2.0) * 0.5 + 0.5;

    float a = iTime * -1.0;
    vec2 pos = vec2(cos(a), sin(a)) * r0;
    float d = distance(uv, pos);
    float v1 = light2(1.5, 5.0, d);
    v1 *= light1(1.0, 50.0, d0);

    float v2 = smoothstep(1.0, mix(innerRadius, 1.0, n0 * 0.5), len);
    float v3 = smoothstep(innerRadius, mix(innerRadius, 1.0, 0.5), len);

    vec3 col = mix(color1, color2, cl);
    col = mix(color3, col, v0);
    col = (col + v1) * v2 * v3;
    col = clamp(col, 0.0, 1.0);

    return extractAlpha(col);
  }

  vec4 mainImage(vec2 fragCoord) {
    vec2 center = iResolution.xy * 0.5;
    float size = min(iResolution.x, iResolution.y);
    vec2 uv = (fragCoord - center) / size * 2.0;

    float angle = rot;
    float s = sin(angle);
    float c = cos(angle);
    uv = vec2(c * uv.x - s * uv.y, s * uv.x + c * uv.y);

    uv.x += hover * hoverIntensity * 0.1 * sin(uv.y * 10.0 + iTime);
    uv.y += hover * hoverIntensity * 0.1 * sin(uv.x * 10.0 + iTime);

    return draw(uv);
  }

  void main() {
    vec2 fragCoord = vUv * iResolution.xy;
    vec4 col = mainImage(fragCoord);
    gl_FragColor = vec4(col.rgb * col.a, col.a);
  }
`;

const BASE_ROTATION_SPEED = 0.3;
const MAX_DPR = 2;

/** The still: the orb's ring in the same colours, drawn by CSS. */
const STILL_GRADIENT =
  "radial-gradient(circle closest-side, transparent 57%, rgba(55, 118, 16, 0.5) 63%, rgba(130, 201, 51, 0.62) 73%, rgba(80, 190, 193, 0.42) 86%, transparent 100%)";

type WebkitAudioWindow = Window & { webkitAudioContext?: typeof AudioContext };

type Gl = WebGLRenderingContext | WebGL2RenderingContext;

function probeContext(canvas: HTMLCanvasElement): Gl | null {
  const attributes: WebGLContextAttributes = {
    alpha: true,
    antialias: true,
    premultipliedAlpha: false,
  };
  try {
    return canvas.getContext("webgl2", attributes) ?? canvas.getContext("webgl", attributes);
  } catch {
    return null;
  }
}

function loseContext(gl: Gl) {
  gl.getExtension("WEBGL_lose_context")?.loseContext();
}

/** Build the scene on a canvas that is known to have a context. `null` if OGL refuses. */
function buildScene(canvas: HTMLCanvasElement) {
  try {
    const renderer = new Renderer({
      canvas,
      alpha: true,
      premultipliedAlpha: false,
      antialias: true,
      dpr: Math.min(window.devicePixelRatio || 1, MAX_DPR),
    });
    const gl = renderer.gl;
    gl.clearColor(0, 0, 0, 0);
    const program = new Program(gl, {
      vertex: VERT,
      fragment: FRAG,
      uniforms: {
        iTime: { value: 0 },
        iResolution: { value: new Vec3(1, 1, 1) },
        hue: { value: 0 },
        hover: { value: 0 },
        rot: { value: 0 },
        hoverIntensity: { value: 0 },
      },
    });
    const mesh = new Mesh(gl, { geometry: new Triangle(gl), program });
    return { renderer, program, mesh };
  } catch {
    return null;
  }
}

function OrbStill() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 rounded-full transition-opacity duration-700 group-data-[state=live]/orb:opacity-0"
      style={{ background: STILL_GRADIENT }}
    />
  );
}

export const VoicePoweredOrb: FC<VoicePoweredOrbProps> = ({
  className,
  hue = 0,
  enableVoiceControl = false,
  voiceSensitivity = 1.5,
  maxRotationSpeed = 1.2,
  maxHoverIntensity = 0.8,
  onVoiceDetected,
}) => {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const samplesRef = useRef<Uint8Array<ArrayBuffer> | null>(null);
  const reduced = useMediaQuery(REDUCED_MOTION_QUERY);

  // The render loop reads the latest props from here instead of restarting when one
  // changes. Rebuilding a WebGL context because `hue` moved is how the original
  // dropped frames — and, with the microphone in the same effect, re-prompted for it.
  const settingsRef = useRef({
    hue,
    voiceSensitivity,
    maxRotationSpeed,
    maxHoverIntensity,
    onVoiceDetected,
  });
  useEffect(() => {
    settingsRef.current = {
      hue,
      voiceSensitivity,
      maxRotationSpeed,
      maxHoverIntensity,
      onVoiceDetected,
    };
  });

  // The microphone: opened only while asked for, by this effect alone.
  useEffect(() => {
    if (!enableVoiceControl || reduced) return;
    let cancelled = false;
    let stream: MediaStream | null = null;
    let audio: AudioContext | null = null;

    const teardown = () => {
      analyserRef.current = null;
      samplesRef.current = null;
      stream?.getTracks().forEach((track) => track.stop());
      stream = null;
      if (audio && audio.state !== "closed") void audio.close().catch(() => {});
      audio = null;
    };

    const open = async () => {
      const media = typeof navigator === "undefined" ? undefined : navigator.mediaDevices;
      if (!media?.getUserMedia) return;
      try {
        stream = await media.getUserMedia({
          audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
        });
        // Every await is a point where the person may already have pressed stop.
        if (cancelled) return teardown();
        const Context = window.AudioContext ?? (window as WebkitAudioWindow).webkitAudioContext;
        if (!Context) return teardown();
        audio = new Context();
        if (audio.state === "suspended") await audio.resume();
        if (cancelled) return teardown();
        const analyser = audio.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.3;
        analyser.minDecibels = -90;
        analyser.maxDecibels = -10;
        audio.createMediaStreamSource(stream).connect(analyser);
        analyserRef.current = analyser;
        samplesRef.current = new Uint8Array(analyser.frequencyBinCount);
      } catch {
        // Denied, no device, or held by another app. The orb keeps breathing without a
        // voice, and whatever asked for the microphone reports the reason in words.
        teardown();
      }
    };

    void open();
    return () => {
      cancelled = true;
      teardown();
    };
  }, [enableVoiceControl, reduced]);

  // The picture.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const reset = () => {
      delete host.dataset.state;
    };

    if (reduced) {
      host.dataset.state = "still";
      return reset;
    }

    const canvas = document.createElement("canvas");
    const probe = probeContext(canvas);
    const scene = probe ? buildScene(canvas) : null;
    if (!probe || !scene) {
      if (probe) loseContext(probe);
      host.dataset.state = "fallback";
      return reset;
    }

    const { renderer, program, mesh } = scene;
    canvas.setAttribute("aria-hidden", "true");
    canvas.className = "pointer-events-none absolute inset-0";
    host.appendChild(canvas);

    const fit = () => {
      const width = host.clientWidth;
      const height = host.clientHeight;
      if (width === 0 || height === 0) return;
      // CSS pixels: OGL applies `dpr` itself.
      renderer.setSize(width, height);
      const drawn = renderer.gl.canvas;
      program.uniforms.iResolution.value.set(drawn.width, drawn.height, drawn.width / drawn.height);
    };
    fit();
    const resizer = typeof ResizeObserver === "function" ? new ResizeObserver(fit) : null;
    resizer?.observe(host);

    let frameId = 0;
    let onScreen = true;
    let last = 0;
    let rotation = 0;
    let hearing = false;

    const readLevel = (sensitivity: number) => {
      const analyser = analyserRef.current;
      const samples = samplesRef.current;
      if (!analyser || !samples) return 0;
      analyser.getByteFrequencyData(samples);
      let sum = 0;
      for (const sample of samples) {
        const value = sample / 255;
        sum += value * value;
      }
      return Math.min(Math.sqrt(sum / samples.length) * sensitivity * 3, 1);
    };

    const frame = (time: number) => {
      frameId = 0;
      if (!onScreen) return;
      const settings = settingsRef.current;
      // Capped, so returning to the page after a pause is not one enormous step.
      const dt = last === 0 ? 0 : Math.min((time - last) / 1000, 0.1);
      last = time;

      const level = readLevel(settings.voiceSensitivity);
      const detected = level > 0.1;
      if (detected !== hearing) {
        hearing = detected;
        settings.onVoiceDetected?.(detected);
      }
      if (level > 0.05) {
        rotation += dt * (BASE_ROTATION_SPEED + level * settings.maxRotationSpeed * 2);
      }

      const uniforms = program.uniforms;
      uniforms.iTime.value = time / 1000;
      uniforms.hue.value = settings.hue;
      uniforms.rot.value = rotation;
      uniforms.hover.value = Math.min(level * 2, 1);
      uniforms.hoverIntensity.value = Math.min(
        level * settings.maxHoverIntensity * 0.8,
        settings.maxHoverIntensity,
      );
      renderer.render({ scene: mesh });
      if (host.dataset.state !== "live") host.dataset.state = "live";
      schedule();
    };

    const schedule = () => {
      if (frameId === 0) frameId = requestAnimationFrame(frame);
    };

    const watcher =
      typeof IntersectionObserver === "function"
        ? new IntersectionObserver((entries) => {
            onScreen = entries.some((entry) => entry.isIntersecting);
            if (onScreen) schedule();
          })
        : null;
    watcher?.observe(host);
    schedule();

    return () => {
      if (frameId !== 0) cancelAnimationFrame(frameId);
      resizer?.disconnect();
      watcher?.disconnect();
      canvas.remove();
      loseContext(renderer.gl);
      reset();
    };
  }, [reduced]);

  return (
    <div ref={hostRef} className={cn("group/orb relative", className)}>
      <OrbStill />
    </div>
  );
};
