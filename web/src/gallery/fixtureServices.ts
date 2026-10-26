import { QueryClient } from "@tanstack/react-query";

import type { EventEnvelope } from "../api/types";
import type { AppServices } from "../app/AppProviders";
import { fetchStreamSnapshot } from "../app/snapshot";
import { createFixtureApi, type FixtureOptions, type FixtureRoutes } from "../fixtures";
import { RealtimeDispatcher, type EventSourceLike } from "../realtime/dispatcher";

const OPEN = 1;
const CLOSED = 2;

/** An event stream that opens at once and only ever carries what `emit` sends it. */
class FixtureEventSource implements EventSourceLike {
  readyState = OPEN;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor() {
    queueMicrotask(() => {
      this.onopen?.(new Event("open"));
    });
  }

  close(): void {
    this.readyState = CLOSED;
  }

  send(envelope: EventEnvelope): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(envelope) }));
  }
}

export interface FixtureServices extends AppServices {
  /** Deliver an event as if the server had streamed it (demo cues, snapshots). */
  emit: (envelope: EventEnvelope) => void;
}

/**
 * The app's services over fixture routes: a typed client, a fresh query cache and a
 * real dispatcher whose stream is local. Account switches and resets in the fixtures
 * go through the same reset path as the live app.
 */
export function createFixtureServices(routes: FixtureRoutes, options: FixtureOptions = {}): FixtureServices {
  const api = createFixtureApi(routes, options);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false, staleTime: Infinity } },
  });
  let source: FixtureEventSource | null = null;
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => fetchStreamSnapshot(api, queryClient),
    openEventSource: () => {
      source = new FixtureEventSource();
      return source;
    },
    openChannel: () => null,
    reload: () => undefined,
  });
  return {
    api,
    queryClient,
    dispatcher,
    emit: (envelope) => {
      source?.send(envelope);
    },
  };
}
