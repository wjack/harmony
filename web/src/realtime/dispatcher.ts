/**
 * The tab's realtime dispatcher: one EventSource per tab, feeding the query cache.
 *
 * Lifecycle (ARCHITECTURE.md §8, §12):
 * - **Open** from a fresh snapshot. `snapshot()` reads `GET /api/session` and its
 *   `X-Harmony-Cursor`; the stream opens at `/api/events?after=<cursor>&epoch=<epoch>`.
 *   The live UI is keyed on `generation`, which bumps only after that, so every query it
 *   then fetches has a cursor at or past the stream's and no event falls between them.
 * - **Durable events** (`seq` set) advance `lastSeq`. Entity snapshots are applied only
 *   when newer than the cached version; demo notices go to their own subscribers and
 *   never touch the cache; stream deltas go to stream subscribers.
 * - **Drops.** The browser reconnects by itself and sends `Last-Event-ID`. If it gives
 *   up (an HTTP error), the dispatcher takes a fresh snapshot, with backoff: if the
 *   session is unchanged it reopens with `?after=<lastSeq>`, keeping the cache;
 *   otherwise it rebuilds as for an act-as.
 * - **Session changes the stream can't report.** A reset, a fork or an act-as in
 *   another world moves the epoch without an event this stream may see; the server ends
 *   the stream and refuses the browser's reconnect (409, the URL's `epoch` is stale), so
 *   it lands in the "gives up" path above and the fresh snapshot shows the new epoch.
 * - **Act-as** (`session.member_changed`, or a successful act-as in this tab): close the
 *   stream, tell the visitor's other tabs to reload, and take a fresh snapshot. The
 *   previous member's cache stays up meanwhile, so the frame keeps showing them rather
 *   than an empty shell; it is cleared in the same synchronous step that primes the new
 *   snapshot and bumps `generation`, so nothing fetched during the switch survives it.
 *   The epoch makes this happen once per switch.
 * - **Locked** (the passcode gate refused a snapshot or any other request): terminal.
 *   Nothing retries, the cache goes, the visitor's other tabs reload (and so lock too),
 *   and the tab leaves for the passcode page, which brings it back once unlocked.
 */
import type { QueryClient } from "@tanstack/react-query";
import { createStore, type StoreApi } from "zustand/vanilla";

import { PasscodeRequired, gatePath } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import type { DemoEvent, EventEnvelope, StreamEvent } from "../api/types";
import { applyEntitySnapshot } from "./cache";
import { parseEnvelope } from "./envelope";

/** What the stream opens from: a snapshot's cursor and the session it was taken in. */
export interface StreamSnapshot {
  /** The snapshot's `X-Harmony-Cursor`; null if the response lacked one. */
  cursor: number | null;
  epoch: number;
  sessionId: string;
  /**
   * Writes what the snapshot fetched (the session itself) into the query cache. Called
   * when the dispatcher adopts the snapshot: after a reset's clear, before the bump.
   */
  prime?: () => void;
}

/** The part of `EventSource` the dispatcher uses (injectable for tests). */
export interface EventSourceLike {
  readonly readyState: number;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent<string>) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(): void;
}

/** The part of `BroadcastChannel` the dispatcher uses (injectable for tests). */
export interface ChannelLike {
  onmessage: ((event: MessageEvent) => void) | null;
  postMessage(message: unknown): void;
  close(): void;
}

export type RealtimeStatus =
  /** Not started. */
  | "idle"
  /** Fetching the first snapshot. */
  | "starting"
  /** The visitor has no session yet: nothing to stream until they start one. */
  | "no-session"
  /** The EventSource is open but the server hasn't answered yet. */
  | "connecting"
  | "open"
  /** The connection dropped; resuming from the last seq. */
  | "reconnecting"
  /** The acting identity or world changed: cache cleared, fetching a fresh snapshot. */
  | "resetting"
  /** The passcode gate refused us; the tab is leaving for the passcode page. */
  | "locked"
  | "stopped";

export interface RealtimeState {
  status: RealtimeStatus;
  /** Bumped each time the cache is rebuilt from a fresh snapshot. Key the live UI on it. */
  generation: number;
  epoch: number | null;
  sessionId: string | null;
  /** The last durable seq applied, or the snapshot's cursor before any event. */
  lastSeq: number | null;
  /** Consecutive failed attempts to reach the API (snapshot or stream); 0 after a success. */
  failures: number;
}

export type DemoListener = (event: DemoEvent, envelope: EventEnvelope) => void;
export type StreamListener = (event: StreamEvent, envelope: EventEnvelope) => void;
export type EnvelopeListener = (envelope: EventEnvelope) => void;
export type LockListener = () => void;
type Unsubscribe = () => void;

export interface DispatcherOptions {
  queryClient: QueryClient;
  /** A fresh snapshot to open the stream from; null when there is no session yet. */
  snapshot: () => Promise<StreamSnapshot | null>;
  openEventSource?: (url: string) => EventSourceLike;
  /** Null where BroadcastChannel is unavailable; other tabs then aren't reloaded. */
  openChannel?: (name: string) => ChannelLike | null;
  reload?: () => void;
  streamUrl?: (after: number | null, epoch: number | null) => string;
  /** Delay before retry number `attempt` (0-based) of a failed snapshot or stream. */
  retryDelayMs?: (attempt: number) => number;
  /** Where the visitor is (path, query and hash), to come back to once they unlock. */
  here?: () => string;
  /** Leaves the app for the passcode page. */
  leave?: (url: string) => void;
}

/** The visitor's tabs share this BroadcastChannel (same origin, so same session). */
export const TAB_CHANNEL = "harmony.session";

/** What one tab tells the others: the acting member changed under `epoch`. */
export interface MemberChangedMessage {
  type: "member_changed";
  epoch: number;
}

/** What one tab tells the others: Harmony is locked, so nothing they hold is usable. */
export interface LockedMessage {
  type: "locked";
}

type TabMessage = MemberChangedMessage | LockedMessage;

// EventSource.readyState values; not read from the global, which Node lacks.
const READY_STATE_CLOSED = 2;

const INITIAL_STATE: RealtimeState = {
  status: "idle",
  generation: 0,
  epoch: null,
  sessionId: null,
  lastSeq: null,
  failures: 0,
};

export function eventStreamUrl(after: number | null, epoch: number | null = null): string {
  const params = new URLSearchParams();
  if (after !== null) params.set("after", String(after));
  if (epoch !== null) params.set("epoch", String(epoch));
  const query = params.toString();
  return query === "" ? "/api/events" : `/api/events?${query}`;
}

/** Exponential backoff from 1 s to 30 s, with jitter so tabs don't retry in lockstep. */
export function backoffDelayMs(attempt: number): number {
  const ceiling = Math.min(30_000, 1_000 * 2 ** attempt);
  return ceiling / 2 + Math.random() * (ceiling / 2);
}

function openBrowserEventSource(url: string): EventSourceLike {
  return new EventSource(url);
}

function openBrowserChannel(name: string): ChannelLike | null {
  return typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(name);
}

function reloadPage(): void {
  window.location.reload();
}

function currentPath(): string {
  return window.location.pathname + window.location.search + window.location.hash;
}

function leavePage(url: string): void {
  window.location.assign(url);
}

function parseTabMessage(data: unknown): TabMessage | null {
  if (typeof data !== "object" || data === null) return null;
  const message = data as { type?: unknown; epoch?: unknown };
  if (message.type === "locked") return { type: "locked" };
  if (message.type !== "member_changed" || typeof message.epoch !== "number") return null;
  return { type: "member_changed", epoch: message.epoch };
}

function notify<A extends unknown[]>(listeners: Iterable<(...args: A) => void>, ...args: A): void {
  for (const listener of listeners) {
    try {
      listener(...args);
    } catch (error) {
      // One failing subscriber (a toast, say) must not stop the stream or the others.
      console.error("realtime: a subscriber threw", error);
    }
  }
}

export class RealtimeDispatcher {
  readonly store: StoreApi<RealtimeState> = createStore<RealtimeState>(() => INITIAL_STATE);

  readonly #queryClient: QueryClient;
  readonly #snapshot: () => Promise<StreamSnapshot | null>;
  readonly #openEventSource: (url: string) => EventSourceLike;
  readonly #openChannel: (name: string) => ChannelLike | null;
  readonly #reload: () => void;
  readonly #streamUrl: (after: number | null, epoch: number | null) => string;
  readonly #retryDelayMs: (attempt: number) => number;
  readonly #here: () => string;
  readonly #leave: (url: string) => void;

  readonly #demoListeners = new Set<DemoListener>();
  readonly #lockListeners = new Set<LockListener>();
  readonly #streamListeners = new Set<StreamListener>();
  readonly #envelopeListeners = new Set<EnvelopeListener>();

  #source: EventSourceLike | null = null;
  #channel: ChannelLike | null = null;
  #retryTimer: ReturnType<typeof setTimeout> | null = null;
  #failures = 0;
  // Set by a reset: the cache belongs to the previous member (or world). It stays up
  // until the next snapshot is adopted, so the frame isn't blanked in the meantime.
  #cacheStale = false;
  // Bumped by every start, reset and stop. Async work started under an older run gives
  // up when it resumes, so a slow snapshot can never reopen a stream that was replaced.
  #run = 0;

  constructor(options: DispatcherOptions) {
    this.#queryClient = options.queryClient;
    this.#snapshot = options.snapshot;
    this.#openEventSource = options.openEventSource ?? openBrowserEventSource;
    this.#openChannel = options.openChannel ?? openBrowserChannel;
    this.#reload = options.reload ?? reloadPage;
    this.#streamUrl = options.streamUrl ?? eventStreamUrl;
    this.#retryDelayMs = options.retryDelayMs ?? backoffDelayMs;
    this.#here = options.here ?? currentPath;
    this.#leave = options.leave ?? leavePage;
  }

  get state(): RealtimeState {
    return this.store.getState();
  }

  /** Open the stream from a fresh snapshot. A no-op while already running, or locked. */
  start(): Promise<void> {
    const { status } = this.state;
    if (status !== "idle" && status !== "no-session" && status !== "stopped") {
      return Promise.resolve();
    }
    this.#listenToOtherTabs();
    return this.#rebuild("starting");
  }

  /** Close the stream and the tab channel. `start()` may be called again later. */
  stop(): void {
    this.#run += 1;
    this.#closeSource();
    this.#cancelRetry();
    if (this.#channel !== null) {
      this.#channel.onmessage = null;
      this.#channel.close();
      this.#channel = null;
    }
    this.#set({ status: "stopped" });
  }

  /**
   * The acting member changed under `epoch`: from `session.member_changed` on the
   * stream, or from a successful act-as in this tab (whichever comes first).
   */
  memberChanged(epoch: number): Promise<void> {
    if (this.state.status === "locked") return Promise.resolve();
    const current = this.state.epoch;
    if (current !== null && epoch <= current) return Promise.resolve();
    this.#set({ epoch });
    const message: MemberChangedMessage = { type: "member_changed", epoch };
    this.#channel?.postMessage(message);
    return this.#reset();
  }

  /** Rebuild from a fresh snapshot without an identity change (the world was retired). */
  resync(): Promise<void> {
    if (this.state.status === "locked") return Promise.resolve();
    return this.#reset();
  }

  /**
   * The passcode gate refused a request. Stop for good, drop everything fetched behind
   * the gate, have the visitor's other tabs reload (they lock too), and leave for the
   * passcode page, which returns here once unlocked. Idempotent.
   */
  lock(): void {
    if (this.state.status === "locked") return;
    this.#run += 1;
    this.#closeSource();
    this.#cancelRetry();
    this.#cacheStale = false;
    this.#setFailures(0);
    this.#set({ status: "locked" });
    this.#queryClient.clear();
    notify(this.#lockListeners);
    const message: LockedMessage = { type: "locked" };
    this.#channel?.postMessage(message);
    this.#leave(gatePath(this.#here()));
  }

  /** Called as the tab locks, after the cache is cleared: for state kept outside it. */
  onLocked(listener: LockListener): Unsubscribe {
    this.#lockListeners.add(listener);
    return () => {
      this.#lockListeners.delete(listener);
    };
  }

  onDemo(listener: DemoListener): Unsubscribe {
    this.#demoListeners.add(listener);
    return () => {
      this.#demoListeners.delete(listener);
    };
  }

  onStream(listener: StreamListener): Unsubscribe {
    this.#streamListeners.add(listener);
    return () => {
      this.#streamListeners.delete(listener);
    };
  }

  /** Every well-formed envelope, after routing. For logs and debugging views. */
  onEnvelope(listener: EnvelopeListener): Unsubscribe {
    this.#envelopeListeners.add(listener);
    return () => {
      this.#envelopeListeners.delete(listener);
    };
  }

  // --- lifecycle ----------------------------------------------------------------------

  #reset(): Promise<void> {
    this.#closeSource();
    this.#cancelRetry();
    this.#cacheStale = true;
    return this.#rebuild("resetting");
  }

  async #rebuild(status: "starting" | "resetting"): Promise<void> {
    this.#run += 1;
    const run = this.#run;
    this.#set({ status });

    let snapshot: StreamSnapshot | null;
    try {
      snapshot = await this.#snapshot();
    } catch (error) {
      if (run !== this.#run) return;
      if (error instanceof PasscodeRequired) {
        this.lock();
        return;
      }
      console.warn("realtime: snapshot failed; retrying", error);
      this.#retryLater(run, () => void this.#rebuild(status));
      return;
    }
    if (run !== this.#run) return;
    this.#setFailures(0);

    // Everything cached was fetched as the previous member (or world), including anything
    // the old frame fetched while the snapshot was in flight. Clearing here, in the same
    // synchronous step as the bump, means the old frame never renders an empty cache.
    if (this.#cacheStale) {
      this.#cacheStale = false;
      this.#queryClient.clear();
    }
    if (snapshot === null) {
      this.#set({ status: "no-session", sessionId: null, lastSeq: null });
      return;
    }
    if (snapshot.cursor === null) {
      console.warn("realtime: snapshot had no X-Harmony-Cursor; events before the stream may be missed");
    }
    snapshot.prime?.();
    this.#set({
      generation: this.state.generation + 1,
      epoch: snapshot.epoch,
      sessionId: snapshot.sessionId,
      lastSeq: snapshot.cursor,
    });
    this.#open();
  }

  #open(): void {
    const { lastSeq, epoch } = this.state;
    const source = this.#openEventSource(this.#streamUrl(lastSeq, epoch));
    this.#source = source;
    this.#set({ status: "connecting" });

    source.onopen = () => {
      if (this.#source !== source) return;
      this.#setFailures(0);
      this.#set({ status: "open" });
    };
    source.onmessage = (event) => {
      if (this.#source !== source) return;
      this.#receive(event.data);
    };
    source.onerror = () => {
      if (this.#source !== source) return;
      this.#set({ status: "reconnecting" });
      if (source.readyState === READY_STATE_CLOSED) {
        // The browser gave up (an HTTP error status, or not an event stream). It may
        // have been refused because the session moved on; check before reopening.
        this.#closeSource();
        this.#retryLater(this.#run, () => void this.#recover());
      }
      // Otherwise the browser is already reconnecting, with Last-Event-ID.
    };
  }

  /** After the browser gave up: reopen from `lastSeq` if the session is unchanged. */
  async #recover(): Promise<void> {
    const run = this.#run;
    let snapshot: StreamSnapshot | null;
    try {
      snapshot = await this.#snapshot();
    } catch (error) {
      if (run !== this.#run) return;
      if (error instanceof PasscodeRequired) {
        // The stream was refused because Harmony is locked, not because the session moved.
        this.lock();
        return;
      }
      console.warn("realtime: snapshot failed; retrying", error);
      this.#retryLater(run, () => void this.#recover());
      return;
    }
    if (run !== this.#run) return;

    const { epoch, sessionId } = this.state;
    if (snapshot !== null && snapshot.sessionId === sessionId && epoch !== null) {
      if (snapshot.epoch === epoch) {
        // Same session, same member: the cache is still valid and nothing is skipped.
        snapshot.prime?.();
        this.#open();
        return;
      }
      if (snapshot.epoch > epoch) {
        // A switch this stream couldn't report (another world's act-as, a reset).
        await this.memberChanged(snapshot.epoch);
        return;
      }
    }
    // No session any more, or a different one: nothing cached belongs to it.
    await this.#reset();
  }

  #closeSource(): void {
    const source = this.#source;
    if (source === null) return;
    this.#source = null;
    source.onopen = null;
    source.onmessage = null;
    source.onerror = null;
    source.close();
  }

  #retryLater(run: number, retry: () => void): void {
    this.#cancelRetry();
    const delay = this.#retryDelayMs(this.#failures);
    this.#setFailures(this.#failures + 1);
    this.#retryTimer = setTimeout(() => {
      this.#retryTimer = null;
      if (run === this.#run) retry();
    }, delay);
  }

  #cancelRetry(): void {
    if (this.#retryTimer !== null) {
      clearTimeout(this.#retryTimer);
      this.#retryTimer = null;
    }
  }

  #listenToOtherTabs(): void {
    if (this.#channel !== null) return;
    const channel = this.#openChannel(TAB_CHANNEL);
    if (channel === null) return;
    channel.onmessage = (event) => {
      const message = parseTabMessage(event.data);
      if (message === null || this.state.status === "locked") return;
      // Another tab locked Harmony: reload, so this one meets the gate too rather than
      // leaving the cast and the story on screen.
      if (message.type === "locked") {
        this.#reload();
        return;
      }
      // Another tab switched accounts. Unless this tab already handled that switch
      // from its own stream, reload it: routes, drafts and caches all belong to the
      // previous member.
      const current = this.state.epoch;
      if (current === null || message.epoch > current) this.#reload();
    };
    this.#channel = channel;
  }

  // --- events -------------------------------------------------------------------------

  #receive(data: string): void {
    const envelope = parseEnvelope(data);
    if (envelope === null) {
      console.warn("realtime: ignoring a malformed event frame");
      return;
    }
    if (envelope.seq !== null) {
      const last = this.state.lastSeq;
      if (last !== null && envelope.seq <= last) return; // already applied
      this.#set({ lastSeq: envelope.seq });
    }
    this.#route(envelope);
    notify(this.#envelopeListeners, envelope);
  }

  #route(envelope: EventEnvelope): void {
    const { event } = envelope;
    switch (event.type) {
      case "entity.snapshot":
        applyEntitySnapshot(this.#queryClient, event);
        return;
      case "thread.access_changed":
        void this.#queryClient.invalidateQueries({ queryKey: queryKeys.threads });
        void this.#queryClient.invalidateQueries({
          queryKey: queryKeys.entity("thread", event.thread_id),
        });
        return;
      case "demo.notice":
      case "demo.needs_human":
        notify(this.#demoListeners, event, envelope);
        return;
      case "stream.delta":
      case "stream.typing":
      case "stream.reset":
        notify(this.#streamListeners, event, envelope);
        return;
      // A conversation's text: the chat reads it off the envelope listeners (chat/live.ts).
      case "stream.chat_delta":
        return;
      // For the escalation panel waiting on that rev, off the envelope listeners (requests/live.ts).
      case "escalation.redraft_failed":
        return;
      case "session.member_changed": {
        const { sessionId } = this.state;
        if (sessionId === null || event.session_id === sessionId) {
          void this.memberChanged(event.epoch);
        }
        return;
      }
      case "world.retired":
        void this.resync();
        return;
      case "world.created":
      case "hello.tick":
        return;
    }
  }

  #setFailures(failures: number): void {
    this.#failures = failures;
    if (this.state.failures !== failures) this.#set({ failures });
  }

  #set(patch: Partial<RealtimeState>): void {
    this.store.setState(patch);
  }
}
