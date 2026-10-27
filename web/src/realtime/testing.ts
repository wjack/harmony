/**
 * Test doubles for the dispatcher's browser dependencies, and envelope builders.
 * Imported only by tests.
 */
import type { EventEnvelope, HarmonyEvent } from "../api/types";
import type { ChannelLike, EventSourceLike } from "./dispatcher";

export const WORLD_ID = "6f1c2c1e-0000-4000-8000-000000000001";
export const SESSION_ID = "6f1c2c1e-0000-4000-8000-0000000000aa";
export const THREAD_ID = "6f1c2c1e-0000-4000-8000-0000000000b1";
export const ORG_ID = "6f1c2c1e-0000-4000-8000-0000000000c1";

const CONNECTING = 0;
const OPEN = 1;
const CLOSED = 2;

export class FakeEventSource implements EventSourceLike {
  readyState = CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  readonly url: string;

  constructor(url: string) {
    this.url = url;
  }

  close(): void {
    this.closed = true;
    this.readyState = CLOSED;
  }

  /** The server accepted the stream. */
  accept(): void {
    this.readyState = OPEN;
    this.onopen?.(new Event("open"));
  }

  send(envelope: EventEnvelope): void {
    this.sendRaw(JSON.stringify(envelope));
  }

  sendRaw(data: string): void {
    this.onmessage?.(new MessageEvent("message", { data }));
  }

  /** The connection dropped and the browser is reconnecting with Last-Event-ID. */
  drop(): void {
    this.readyState = CONNECTING;
    this.onerror?.(new Event("error"));
  }

  /** The browser gave up, as it does on an HTTP error status. */
  fail(): void {
    this.readyState = CLOSED;
    this.onerror?.(new Event("error"));
  }
}

/** Records every EventSource a dispatcher opens. */
export class EventSources {
  readonly opened: FakeEventSource[] = [];

  readonly open = (url: string): FakeEventSource => {
    const source = new FakeEventSource(url);
    this.opened.push(source);
    return source;
  };

  get latest(): FakeEventSource {
    const source = this.opened.at(-1);
    if (source === undefined) throw new Error("no EventSource was opened");
    return source;
  }
}

/**
 * BroadcastChannel semantics within one test: a message reaches every other open
 * channel of the same name, never the sender. Delivery is synchronous, for clarity.
 */
export class ChannelHub {
  readonly #channels = new Set<FakeChannel>();

  readonly open = (name: string): FakeChannel => {
    const channel = new FakeChannel(name, this);
    this.#channels.add(channel);
    return channel;
  };

  deliver(from: FakeChannel, message: unknown): void {
    for (const channel of this.#channels) {
      if (channel !== from && channel.name === from.name) {
        channel.onmessage?.(new MessageEvent("message", { data: structuredClone(message) }));
      }
    }
  }

  remove(channel: FakeChannel): void {
    this.#channels.delete(channel);
  }
}

export class FakeChannel implements ChannelLike {
  onmessage: ((event: MessageEvent) => void) | null = null;
  readonly posted: unknown[] = [];
  readonly name: string;
  readonly #hub: ChannelHub;

  constructor(name: string, hub: ChannelHub) {
    this.name = name;
    this.#hub = hub;
  }

  postMessage(message: unknown): void {
    this.posted.push(message);
    this.#hub.deliver(this, message);
  }

  close(): void {
    this.#hub.remove(this);
  }
}

// --- envelopes ----------------------------------------------------------------------

type Scope = EventEnvelope["scope"];

export const worldScope: Scope = {
  kind: "world",
  org_id: null,
  member_id: null,
  thread_id: null,
  session_id: null,
};

export function durable(seq: number, event: HarmonyEvent, scope: Scope = worldScope): EventEnvelope {
  return {
    world_id: WORLD_ID,
    seq,
    created_at: "2026-10-26T09:00:00Z",
    scope,
    event,
  };
}

export function ephemeral(event: HarmonyEvent): EventEnvelope {
  return { world_id: WORLD_ID, seq: null, created_at: null, scope: worldScope, event };
}

export function threadSnapshot(seq: number, version: number, state: string): EventEnvelope {
  return durable(
    seq,
    {
      type: "entity.snapshot",
      entity_type: "thread",
      entity_id: THREAD_ID,
      version,
      data: { state },
    },
    { ...worldScope, kind: "thread", org_id: ORG_ID, thread_id: THREAD_ID },
  );
}

export function memberChanged(seq: number, epoch: number, sessionId = SESSION_ID): EventEnvelope {
  return durable(
    seq,
    {
      type: "session.member_changed",
      session_id: sessionId,
      member_id: "6f1c2c1e-0000-4000-8000-0000000000d1",
      epoch,
    },
    { ...worldScope, kind: "session", session_id: sessionId },
  );
}

export function demoNotice(seq: number, kind: string): EventEnvelope {
  return durable(
    seq,
    { type: "demo.notice", kind, org_slug: "northlake", entity_id: null },
    { ...worldScope, kind: "demo" },
  );
}

/** A needs-human cue; cues committed together (one routing) share `committedAt`. */
export function needsHuman(
  seq: number,
  memberSlug: string,
  entityId: string,
  orgSlug = "northlake",
  committedAt = "2026-10-26T09:00:00Z",
): EventEnvelope {
  const envelope = durable(
    seq,
    {
      type: "demo.needs_human",
      kind: "needs_human",
      org_slug: orgSlug,
      member_slug: memberSlug,
      entity_id: entityId,
    },
    { ...worldScope, kind: "demo" },
  );
  return { ...envelope, created_at: committedAt };
}
