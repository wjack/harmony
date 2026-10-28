/**
 * Keeps the network view live (ARCHITECTURE.md §8, §11.6).
 *
 The network view is a demo tool (opened from the demo bar) that watches both sides of
 * every thread, in either world, so it opens its own `GET /api/events?lens=network`
 * stream from the view's snapshot cursor while it is mounted. The tab's own stream
 * stays the acting member's.
 *
 * Snapshots that change the graph refetch the view (a composed server view, refetched
 * rather than patched); typing marks a thread's edge as in flight; a committed message
 * pulses it.
 */
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { createContext, use, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";

import type { EventEnvelope } from "../api/types";
import type { EventSourceLike } from "../realtime/dispatcher";
import { parseEnvelope } from "../realtime/envelope";
import { networkKeys } from "./api";
import type { WorldLens } from "./types";

export type OpenLensSource = (url: string) => EventSourceLike;

function openBrowserSource(url: string): EventSourceLike {
  return new EventSource(url);
}

/** How the lens stream is opened; tests and the gallery swap in a scripted source. */
export const LensSourceContext = createContext<OpenLensSource | null>(openBrowserSource);

export function lensStreamUrl(world: WorldLens, after: number | null, epoch: number | null): string {
  const params = new URLSearchParams({ lens: "network", world });
  if (after !== null) params.set("after", String(after));
  if (epoch !== null) params.set("epoch", String(epoch));
  return `/api/events?${params.toString()}`;
}

/** Snapshots of these change a node's status, an edge's state or its label. */
export const GRAPH_ENTITY_TYPES: ReadonlySet<string> = new Set([
  "thread",
  "thread_message",
  "network_request",
  "consult",
  "escalation",
]);

export interface LensSignal {
  /** Refetch the view. */
  refresh: boolean;
  /** A turn is streaming on this thread. */
  typing: string | null;
  /** A message was committed on this thread. */
  pulse: string | null;
}

const NOTHING: LensSignal = { refresh: false, typing: null, pulse: null };

/** What one envelope means for the canvas. */
export function lensSignal(envelope: EventEnvelope): LensSignal {
  const { event } = envelope;
  switch (event.type) {
    case "entity.snapshot": {
      if (!GRAPH_ENTITY_TYPES.has(event.entity_type)) return NOTHING;
      const threadId = event.entity_type === "thread_message" ? event.data.thread_id : null;
      return { refresh: true, typing: null, pulse: typeof threadId === "string" ? threadId : null };
    }
    // A needs-human cue is how the desk hears that another org's thread now waits on
    // its team: that org's escalation snapshot isn't the desk's to see.
    case "thread.access_changed":
    case "demo.needs_human":
    case "world.retired":
      return { ...NOTHING, refresh: true };
    case "stream.typing":
    case "stream.delta":
      return { ...NOTHING, typing: event.thread_id };
    default:
      return NOTHING;
  }
}

/**
 * How long an edge keeps animating after the last typing frame. The worker sends one
 * every 5 s for a whole turn, thinking included (LIVE_DEMO_PLAN 3.5), so one late frame
 * doesn't stop it.
 */
export const TYPING_TTL_MS = 8_000;
const REFRESH_DEBOUNCE_MS = 250;
export const RETRY_MIN_MS = 2_000;
const RETRY_MAX_MS = 30_000;
const READY_STATE_CLOSED = 2;

export interface LensActivity {
  /** Threads with a turn in flight: their edges animate. */
  inFlight: ReadonlySet<string>;
  /** Per thread, a counter that bumps on each committed message: the edge pulses. */
  pulses: ReadonlyMap<string, number>;
}

interface LensOptions {
  world: WorldLens;
  /** The world the view was read from; a new one (a reset) restarts the stream. */
  worldId: string | null;
  /** The view's snapshot cursor; the lens stream opens from it once it is known. */
  cursor: number | null;
  epoch: number | null;
  enabled: boolean;
}

type InFlight = ReadonlyMap<string, number>;
type Pulses = ReadonlyMap<string, number>;

/**
 * Applies envelopes to the canvas state, refetching the view at most every 250 ms.
 * `dispose` cancels a pending refetch.
 */
function canvasSink(
  queryClient: QueryClient,
  world: WorldLens,
  setInFlight: Dispatch<SetStateAction<InFlight>>,
  setPulses: Dispatch<SetStateAction<Pulses>>,
) {
  let refreshTimer: ReturnType<typeof setTimeout> | null = null;

  const refresh = () => {
    if (refreshTimer !== null) return;
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      void queryClient.invalidateQueries({ queryKey: networkKeys.world(world) });
    }, REFRESH_DEBOUNCE_MS);
  };

  const receive = (envelope: EventEnvelope) => {
    const signal = lensSignal(envelope);
    if (signal.refresh) refresh();
    const { typing, pulse } = signal;
    if (typing !== null) {
      setInFlight((current) => new Map(current).set(typing, Date.now() + TYPING_TTL_MS));
    }
    if (pulse !== null) {
      setPulses((current) => new Map(current).set(pulse, (current.get(pulse) ?? 0) + 1));
      // The committed message ends that turn's stream.
      setInFlight((current) => {
        if (!current.has(pulse)) return current;
        const next = new Map(current);
        next.delete(pulse);
        return next;
      });
    }
  };

  const dispose = () => {
    if (refreshTimer !== null) clearTimeout(refreshTimer);
  };

  return { receive, refresh, dispose };
}

/** Subscribe the view to its world's stream while mounted (module docstring). */
export function useNetworkLive({ world, worldId, cursor, epoch, enabled }: LensOptions): LensActivity {
  const open = use(LensSourceContext);
  const queryClient = useQueryClient();
  const [inFlight, setInFlight] = useState<InFlight>(new Map());
  const [pulses, setPulses] = useState<Pulses>(new Map());

  // Where a reopened lens stream resumes: the latest durable seq seen in the world it
  // was read from. Seqs belong to one world, so another world starts over from its
  // own snapshot cursor rather than carrying a seq across.
  const resume = useRef<{ worldId: string | null; seq: number | null }>({ worldId: null, seq: null });
  const latestCursor = useRef(cursor);
  useEffect(() => {
    latestCursor.current = cursor;
  }, [cursor]);

  // The demo lens.
  const lensReady = enabled && cursor !== null && worldId !== null && open !== null;
  useEffect(() => {
    if (!lensReady) return;
    if (resume.current.worldId !== worldId) resume.current = { worldId, seq: latestCursor.current };
    const sink = canvasSink(queryClient, world, setInFlight, setPulses);
    let source: EventSourceLike | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;
    let closed = false;

    const connect = () => {
      if (closed) return;
      const current = open(lensStreamUrl(world, resume.current.seq, epoch));
      source = current;
      current.onopen = () => {
        failures = 0;
      };
      current.onmessage = (event) => {
        const envelope = parseEnvelope(event.data);
        if (envelope === null) return;
        if (envelope.seq !== null) resume.current.seq = Math.max(resume.current.seq ?? 0, envelope.seq);
        sink.receive(envelope);
      };
      current.onerror = () => {
        // EventSource reconnects by itself (with Last-Event-ID) unless the server
        // refused it: a moved epoch (an act-as or reset), a reset world, no session.
        // Re-read the session and the view; a new epoch or world restarts this effect
        // from the fresh snapshot, and an unchanged one resumes where it left off.
        if (current.readyState !== READY_STATE_CLOSED || closed) return;
        current.close();
        void queryClient.invalidateQueries({ queryKey: networkKeys.session(world) });
        sink.refresh();
        const delay = Math.min(RETRY_MAX_MS, RETRY_MIN_MS * 2 ** failures);
        failures += 1;
        retryTimer = setTimeout(() => {
          retryTimer = null;
          connect();
        }, delay);
      };
    };

    connect();
    return () => {
      closed = true;
      source?.close();
      if (retryTimer !== null) clearTimeout(retryTimer);
      sink.dispose();
    };
  }, [lensReady, open, world, worldId, epoch, queryClient]);

  // Drop in-flight marks once their typing goes quiet.
  useEffect(() => {
    if (inFlight.size === 0) return;
    const timer = setInterval(() => {
      const now = Date.now();
      setInFlight((current) => {
        const next = new Map([...current].filter(([, expires]) => expires > now));
        return next.size === current.size ? current : next;
      });
    }, 500);
    return () => {
      clearInterval(timer);
    };
  }, [inFlight.size]);

  const inFlightIds = useMemo(() => new Set(inFlight.keys()), [inFlight]);
  return { inFlight: inFlightIds, pulses };
}
