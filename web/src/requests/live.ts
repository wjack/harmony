/**
 * Keeps the inbox and the open thread current from the tab's event stream.
 *
 * The dispatcher caches each entity snapshot under `entity(type, id)`, but the inbox
 * and the thread view are composed server-side views (sections, drafts, what our
 * representative saw), so a relevant snapshot refetches them instead of patching them.
 * Token deltas drive the "is typing" row, and a failed redraft frees the panel waiting
 * on it; neither is cached.
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

/**
 * The redraft revs of `escalationId` that the worker reported it wrote no draft for
 * (`escalation.redraft_failed`), so the panel stops waiting on them at once.
 */
export function useFailedRedrafts(escalationId: string): ReadonlySet<number> {
  const dispatcher = use(DispatcherContext);
  const [failed, setFailed] = useState<{ escalationId: string; revs: ReadonlySet<number> }>({
    escalationId,
    revs: new Set(),
  });

  useEffect(() => {
    if (dispatcher === null) return;
    return dispatcher.onEnvelope(({ event }) => {
      if (event.type !== "escalation.redraft_failed" || event.escalation_id !== escalationId) return;
      setFailed((current) => ({
        escalationId,
        revs: new Set([...(current.escalationId === escalationId ? current.revs : []), event.draft_rev]),
      }));
    });
  }, [dispatcher, escalationId]);

  // Revs from a previous item mean nothing for this one.
  return failed.escalationId === escalationId ? failed.revs : NONE;
}

const NONE: ReadonlySet<number> = new Set();

const ENDED: ReadonlySet<unknown> = new Set(["closed", "expired"]);

/**
 * Whether `envelope` ends the "is typing" row in `threadId`: the committed message
 * replaces the streamed text, and a thread that ended has no turn still running.
 */
export function endsTyping(envelope: EventEnvelope, threadId: string): boolean {
  const { event } = envelope;
  if (event.type !== "entity.snapshot") return false;
  if (event.entity_type === "thread_message") return event.data.thread_id === threadId;
  return event.entity_type === "thread" && event.entity_id === threadId && ENDED.has(event.data.state);
}

export interface Typing {
  threadId: string;
  orgId: string;
  turnId: string;
  /** Our own representative's streamed text; empty for the counterparty's (we see only that it types). */
  text: string;
}

// A turn's last delta can be followed by its committed message only after a model
// call; past this, the row is stale (the turn failed or moved on).
const TYPING_TIMEOUT_MS = 20_000;

/** Who is writing in `threadId` right now, from `stream.typing` and `stream.delta`. */
export function useTyping(threadId: string | undefined): Typing | null {
  const dispatcher = use(DispatcherContext);
  const [typing, setTyping] = useState<Typing | null>(null);

  useEffect(() => {
    if (dispatcher === null || threadId === undefined) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const clearLater = () => {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        setTyping(null);
      }, TYPING_TIMEOUT_MS);
    };

    const offStream = dispatcher.onStream((event) => {
      if (event.thread_id !== threadId) return;
      switch (event.type) {
        case "stream.typing":
          setTyping((current) =>
            current?.turnId === event.turn_id
              ? current
              : { threadId, orgId: event.org_id, turnId: event.turn_id, text: "" },
          );
          break;
        case "stream.delta":
          setTyping((current) => ({
            threadId,
            orgId: event.org_id,
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
      if (endsTyping(envelope, threadId)) setTyping(null);
    });

    return () => {
      offStream();
      offEnvelope();
      if (timer !== null) clearTimeout(timer);
    };
  }, [dispatcher, threadId]);

  // State from a previous thread is stale the moment the thread changes.
  return typing?.threadId === threadId ? typing : null;
}
