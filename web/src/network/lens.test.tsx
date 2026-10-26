// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EventEnvelope, HarmonyEvent } from "../api/types";
import { DispatcherContext } from "../app/context";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { EventSources } from "../realtime/testing";
import { networkKeys } from "./api";
import { LensSourceContext, lensStreamUrl, RETRY_MIN_MS, useNetworkLive } from "./lens";
import type { WorldLens } from "./types";

interface Props {
  world: WorldLens;
  worldId: string | null;
  cursor: number | null;
  epoch: number | null;
}

const SCOPE: EventEnvelope["scope"] = { kind: "world", org_id: null, member_id: null, thread_id: null, session_id: null };

function envelope(seq: number | null, event: HarmonyEvent): EventEnvelope {
  return { world_id: "w", seq, created_at: null, scope: SCOPE, event };
}

const typing = (threadId: string): HarmonyEvent => ({ type: "stream.typing", org_id: "o", thread_id: threadId, turn_id: "t" });

function setup(initial: Props) {
  const queryClient = new QueryClient();
  const lens = new EventSources();
  const tab = new EventSources();
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => Promise.resolve({ cursor: 10, epoch: 1, sessionId: "s" }),
    openEventSource: tab.open,
    openChannel: () => null,
    reload: vi.fn(),
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <DispatcherContext value={dispatcher}>
        <LensSourceContext value={lens.open}>{children}</LensSourceContext>
      </DispatcherContext>
    </QueryClientProvider>
  );
  const hook = renderHook((props: Props) => useNetworkLive({ ...props, enabled: true }), {
    initialProps: initial,
    wrapper,
  });
  return { hook, lens, tab, dispatcher, queryClient };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useNetworkLive", () => {
  it("opens the big run's lens from its own snapshot, never from another world's seq", () => {
    const { hook, lens } = setup({ world: "network", worldId: "net-a", cursor: 500, epoch: 1 });
    expect(lens.latest.url).toBe(lensStreamUrl("network", 500, 1));
    act(() => {
      lens.latest.accept();
      lens.latest.send(envelope(510, typing("t1")));
    });
    expect(hook.result.current.inFlight.has("t1")).toBe(true);

    // A reset: a new epoch, then a new world whose seqs are lower.
    hook.rerender({ world: "network", worldId: "net-a", cursor: 500, epoch: 2 });
    expect(lens.latest.url).toBe(lensStreamUrl("network", 510, 2));
    hook.rerender({ world: "network", worldId: "net-b", cursor: 120, epoch: 2 });
    expect(lens.latest.url).toBe(lensStreamUrl("network", 120, 2));
    expect(lens.opened.slice(0, -1).every((source) => source.closed)).toBe(true);
  });

  it("follows the desk on the tab's own stream, then opens the lens for the big run from its cursor", async () => {
    const { hook, lens, tab, dispatcher } = setup({ world: "desk", worldId: "desk-1", cursor: 500, epoch: 1 });
    await act(() => dispatcher.start());
    act(() => {
      tab.latest.accept();
      tab.latest.send(envelope(11, typing("desk-thread")));
    });
    expect(lens.opened).toHaveLength(0);
    expect(hook.result.current.inFlight.has("desk-thread")).toBe(true);

    hook.rerender({ world: "network", worldId: "net-a", cursor: 120, epoch: 1 });
    expect(lens.opened.map((source) => source.url)).toEqual([lensStreamUrl("network", 120, 1)]);
    // The desk's stream no longer drives the canvas.
    act(() => {
      tab.latest.send(envelope(12, typing("after-switch")));
    });
    expect(hook.result.current.inFlight.has("after-switch")).toBe(false);
    dispatcher.stop();
  });

  it("re-reads the session and the view when the server refuses the lens, then reopens", () => {
    vi.useFakeTimers();
    const { lens, queryClient } = setup({ world: "network", worldId: "net-a", cursor: 40, epoch: 3 });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    act(() => {
      lens.latest.accept();
      lens.latest.send(envelope(44, typing("t1")));
      lens.latest.fail();
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: networkKeys.session("network") });
    act(() => {
      vi.advanceTimersByTime(RETRY_MIN_MS);
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: networkKeys.world("network") });
    expect(lens.opened).toHaveLength(2);
    expect(lens.latest.url).toBe(lensStreamUrl("network", 44, 3));
  });
});
