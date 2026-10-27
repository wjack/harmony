/**
 * Keeps the inbox and the open thread current from the tab's event stream.
 *
 * The dispatcher caches each entity snapshot under `entity(type, id)`, but the inbox
 * and the thread view are composed server-side views (sections, drafts, what our
 * representative saw), so a relevant snapshot refetches them instead of patching them.
 * Token deltas drive our representative's "is typing" row, which isn't cached.
 */
import { useQueryClient } from "@tanstack/react-query";
import { use, useEffect, useState } from "react";

import { queryKeys } from "../api/queryKeys";
import type { EventEnvelope } from "../api/types";
import { DispatcherContext } from "../app/context";

/** Entity types whose snapshots can change an inbox section or a thread view. */
const THREAD_ENTITY_TYPES: ReadonlySet<string> = new Set([
  "thread",
  "thread_message",
  "consult",
  "escalation",
  "escalation_draft",
  "internal_note",
  "network_request",
  "steer",
  "rep_guidance",
]);

/** The thread an entity belongs to, when its snapshot says so. */
function threadOf(entityType: string, entityId: string, data: Record<string, unknown>): string | null {
  if (entityType === "thread") return entityId;
  const threadId = data.thread_id;
  return typeof threadId === "string" ? threadId : null;
}

/** What to refetch for one envelope: the inbox, and threads (null = any open thread). */
export function affectedThreads(envelope: EventEnvelope): { inbox: boolean; threads: (string | null)[] } {
  const { event } = envelope;
  switch (event.type) {
    case "entity.snapshot": {
      if (!THREAD_ENTITY_TYPES.has(event.entity_type)) return { inbox: false, threads: [] };
      const threadId = threadOf(event.entity_type, event.entity_id, event.data);
      return { inbox: true, threads: [threadId] };
    }
    case "thread.access_changed":
      return { inbox: true, threads: [event.thread_id] };
    default:
      return { inbox: false, threads: [] };
  }
}

/** Refetch the inbox and `threadId`'s views when the stream reports a change. */
export function useRequestsLive(threadId: string | undefined): void {
  const dispatcher = use(DispatcherContext);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (dispatcher === null) return;
    return dispatcher.onEnvelope((envelope) => {
      const { inbox, threads } = affectedThreads(envelope);
      if (inbox) void queryClient.invalidateQueries({ queryKey: queryKeys.inbox });
      if (threadId === undefined) return;
      // A snapshot without a thread id (an escalation draft) may still be this thread's.
      if (threads.some((id) => id === null || id === threadId)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.thread(threadId) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.workspace(threadId) });
      }
    });
  }, [dispatcher, queryClient, threadId]);
}

const ENDED: ReadonlySet<unknown> = new Set(["closed", "expired"]);

/** Snapshots that end our turn's writing: what it sent, or the team it went to ask. */
const TURN_RESULTS: ReadonlySet<string> = new Set(["thread_message", "consult", "escalation"]);

/**
 * Whether `envelope` ends the "is typing" row in `threadId`: the committed message
 * replaces the streamed text, a consult or an item for the team means our Claude is
 * waiting on us now, and a thread that ended has no turn still running.
 */
export function endsTyping(envelope: EventEnvelope, threadId: string): boolean {
  const { event } = envelope;
  if (event.type !== "entity.snapshot") return false;
  if (TURN_RESULTS.has(event.entity_type)) return event.data.thread_id === threadId;
  return event.entity_type === "thread" && event.entity_id === threadId && ENDED.has(event.data.state);
}

export interface Typing {
  threadId: string;
  turnId: string;
  /** Our representative's streamed text so far; empty until its first delta. */
  text: string;
}

// A turn's last delta can be followed by its committed message only after a model
// call; past this, the row is stale (the turn failed or moved on).
const TYPING_TIMEOUT_MS = 20_000;

/**
 * Our representative writing in `threadId` right now, from `stream.typing` and
 * `stream.delta`. The counterparty's typing is ignored: while the move is theirs the
 * view says so in one steady line, whatever their Claude is doing.
 */
export function useTyping(threadId: string | undefined, ourOrgId: string | undefined): Typing | null {
  const dispatcher = use(DispatcherContext);
  const [typing, setTyping] = useState<Typing | null>(null);

  useEffect(() => {
    if (dispatcher === null || threadId === undefined || ourOrgId === undefined) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    // The worker flushes a turn's last delta, and may send a typing heartbeat, after the
    // turn's message is committed, and the two travel apart; once the committed result
    // is in, that turn's stream would only bring back a row repeating its tail.
    let shown: string | null = null;
    let ended: string | null = null;
    const clearLater = () => {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        setTyping(null);
      }, TYPING_TIMEOUT_MS);
    };

    const offStream = dispatcher.onStream((event) => {
      if (event.thread_id !== threadId || event.org_id !== ourOrgId) return;
      if (event.turn_id === ended) return;
      shown = event.turn_id;
      switch (event.type) {
        case "stream.typing":
          setTyping((current) =>
            current?.turnId === event.turn_id
              ? current
              : { threadId, turnId: event.turn_id, text: "" },
          );
          break;
        case "stream.delta":
          setTyping((current) => ({
            threadId,
            turnId: event.turn_id,
            text: current?.turnId === event.turn_id ? current.text + event.text : event.text,
          }));
          break;
        case "stream.reset":
          setTyping((current) => (current?.turnId === event.turn_id ? { ...current, text: "" } : current));
          break;
      }
      clearLater();
    });
    const offEnvelope = dispatcher.onEnvelope((envelope) => {
      if (!endsTyping(envelope, threadId)) return;
      ended = shown;
      setTyping(null);
    });

    return () => {
      offStream();
      offEnvelope();
      if (timer !== null) clearTimeout(timer);
    };
  }, [dispatcher, threadId, ourOrgId]);

  // State from a previous thread is stale the moment the thread changes.
  return typing?.threadId === threadId ? typing : null;
}
