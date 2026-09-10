import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { VoicePoweredOrb } from "@/components/ui/voice-powered-orb";
import { REDUCED_MOTION_QUERY } from "@/lib/use-media-query";

/**
 * The orb's obligations that are not about how it looks.
 *
 * jsdom has no WebGL, which is the point of the first test rather than an obstacle:
 * a browser without it must get the still, not an exception. The rest is the
 * microphone — opened only when asked, once, and closed on every path out, including
 * the one where the person pressed stop before the permission prompt was answered.
 */

let contexts = 0;

class FakeAudioContext {
  state: AudioContextState = "running";
  readonly close = vi.fn(async () => {
    this.state = "closed";
  });
  readonly resume = vi.fn(async () => {});

  constructor() {
    contexts += 1;
  }

  createAnalyser() {
    return {
      fftSize: 0,
      smoothingTimeConstant: 0,
      minDecibels: 0,
      maxDecibels: 0,
      frequencyBinCount: 8,
      getByteFrequencyData: vi.fn(),
    };
  }

  createMediaStreamSource() {
    return { connect: vi.fn() };
  }
}

function microphone() {
  const track = { stop: vi.fn() };
  const stream = { getTracks: () => [track] } as unknown as MediaStream;
  const getUserMedia = vi.fn<(constraints: MediaStreamConstraints) => Promise<MediaStream>>();
  getUserMedia.mockResolvedValue(stream);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
  return { track, stream, getUserMedia };
}

beforeEach(() => {
  contexts = 0;
  // jsdom has no WebGL. Stubbed rather than left to jsdom, which would also print a
  // "not implemented" error for every canvas the orb probes.
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  vi.stubGlobal("AudioContext", FakeAudioContext);
});

afterEach(() => {
  delete (navigator as unknown as { mediaDevices?: unknown }).mediaDevices;
});

function host(container: HTMLElement): HTMLElement {
  const element = container.firstElementChild;
  if (!(element instanceof HTMLElement)) throw new Error("the orb rendered nothing");
  return element;
}

describe("without WebGL", () => {
  it("shows the still instead of throwing", async () => {
    const { container } = render(<VoicePoweredOrb className="size-10" />);

    await waitFor(() => expect(host(container)).toHaveAttribute("data-state", "fallback"));
    expect(container.querySelector("canvas")).toBeNull();
  });
});

describe("the microphone", () => {
  it("is never opened unless asked for", async () => {
    const { getUserMedia } = microphone();

    render(<VoicePoweredOrb />);
    await act(async () => {});

    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it("is opened once when asked for, and every track stops when the asking stops", async () => {
    const { getUserMedia, track } = microphone();

    const { rerender } = render(<VoicePoweredOrb enableVoiceControl />);
    await waitFor(() => expect(contexts).toBe(1));
    expect(getUserMedia).toHaveBeenCalledTimes(1);

    rerender(<VoicePoweredOrb enableVoiceControl={false} />);

    expect(track.stop).toHaveBeenCalledTimes(1);
  });

  it("stops every track when the orb goes", async () => {
    const { track } = microphone();

    const { unmount } = render(<VoicePoweredOrb enableVoiceControl />);
    await waitFor(() => expect(contexts).toBe(1));
    unmount();

    expect(track.stop).toHaveBeenCalledTimes(1);
  });

  it("stops a stream that arrives after the person already pressed stop", async () => {
    const { getUserMedia, stream, track } = microphone();
    let grant: (value: MediaStream) => void = () => {};
    getUserMedia.mockReturnValue(
      new Promise<MediaStream>((resolve) => {
        grant = resolve;
      }),
    );

    const { rerender } = render(<VoicePoweredOrb enableVoiceControl />);
    rerender(<VoicePoweredOrb enableVoiceControl={false} />);
    await act(async () => {
      grant(stream);
    });

    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(contexts).toBe(0);
  });

  it("survives a refusal without throwing", async () => {
    const { getUserMedia } = microphone();
    getUserMedia.mockRejectedValue(new DOMException("denied", "NotAllowedError"));

    const { container } = render(<VoicePoweredOrb enableVoiceControl />);
    await act(async () => {});

    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(contexts).toBe(0);
    expect(host(container)).toBeInTheDocument();
  });
});

describe("under reduced motion", () => {
  it("neither draws nor listens", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query === REDUCED_MOTION_QUERY,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    const { getUserMedia } = microphone();
    const getContext = vi.mocked(HTMLCanvasElement.prototype.getContext);

    const { container } = render(<VoicePoweredOrb enableVoiceControl />);
    await act(async () => {});

    expect(host(container)).toHaveAttribute("data-state", "still");
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(getContext).not.toHaveBeenCalled();
  });
});
