/**
 * Keeps the chat live from the tab's event stream (ARCHITECTURE.md §8).
 *
 * Entity snapshots name what changed; the chat refetches the views built from those
 * entities (the conversation's items, a request's per-recipient status) instead of
 * patching them, because both are server-side joins. Token deltas stream into the
 * turn in flight.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { queryKeys } from "../api/queryKeys";
import type { EntitySnapshot, EventEnvelope } from "../api/types";
import { useDispatcher } from "../app/context";
import { committedSteps } from "./items";
import { applyDelta, visibleText, type TurnStream } from "./stream";
import type { ChatDelta, ChatItem, RequestStatusView } from "./types";

function snapshotOf(envelope: EventEnvelope): EntitySnapshot | null {
  return envelope.event.type === "entity.snapshot" ? envelope.event : null;
}

function field(snapshot: EntitySnapshot, name: string): string | null {
  const value = snapshot.data[name];
  return typeof value === "string" ? value : null;
}

/** The dispatcher hands `stream.chat_delta` to envelope listeners: only the chat reads it. */
export function chatDeltaOf(envelope: EventEnvelope): ChatDelta | null {
  return envelope.event.type === "stream.chat_delta" ? envelope.event : null;
}

/** The escalations the conversation's inline questions show; their snapshots change the items. */
export function watchedIds(items: readonly ChatItem[]): string[] {
  return items.flatMap((item) => (item.type === "inline_question" ? [item.escalation_id] : [])).sort();
}

export function concernsConversation(
  snapshot: EntitySnapshot,
  conversationId: string,
  watched: ReadonlySet<string>,
): boolean {
  switch (snapshot.entity_type) {
    case "conversation":
      return snapshot.entity_id === conversationId;
    // The org's send-mode default, which a chat without its own override follows.
    // Another org's policy reaches only a lens viewer, and costs just a refetch.
    case "rep_policy":
      return true;
    case "card":
    case "network_request":
      return field(snapshot, "conversation_id") === conversationId;
    case "escalation":
      return watched.has(snapshot.entity_id);
    default:
      return false;
  }
}

export function concernsRequest(
  snapshot: EntitySnapshot,
  requestId: string,
  threadIds: ReadonlySet<string>,
): boolean {
  switch (snapshot.entity_type) {
    case "network_request":
      return snapshot.entity_id === requestId;
    case "thread":
      return threadIds.has(snapshot.entity_id) || field(snapshot, "request_id") === requestId;
    case "escalation":
    case "consult": {
      const threadId = field(snapshot, "thread_id");
      return threadId !== null && threadIds.has(threadId);
    }
    default:
      return false;
  }
}

export function useConversationLive(conversationId: string, items: readonly ChatItem[] | undefined): void {
  const dispatcher = useDispatcher();
  const queryClient = useQueryClient();
  // A string, so the subscription only changes when the watched set does.
  const watchKey = watchedIds(items ?? []).join(",");

  useEffect(() => {
    const watched = new Set(watchKey === "" ? [] : watchKey.split(","));
    return dispatcher.onEnvelope((envelope) => {
      const snapshot = snapshotOf(envelope);
      if (snapshot !== null && concernsConversation(snapshot, conversationId, watched)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.conversation(conversationId) });
      }
    });
  }, [dispatcher, queryClient, conversationId, watchKey]);
}

export function useRequestLive(requestId: string, view: RequestStatusView | undefined): void {
  const dispatcher = useDispatcher();
  const queryClient = useQueryClient();
  const threadKey = (view?.recipients ?? [])
    .flatMap((recipient) => (recipient.thread_id === null ? [] : [recipient.thread_id]))
    .sort()
    .join(",");

  useEffect(() => {
    const threadIds = new Set(threadKey === "" ? [] : threadKey.split(","));
    return dispatcher.onEnvelope((envelope) => {
      const snapshot = snapshotOf(envelope);
      if (snapshot !== null && concernsRequest(snapshot, requestId, threadIds)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.request(requestId) });
      }
    });
  }, [dispatcher, queryClient, requestId, threadKey]);
}

export interface StreamingText {
  turnId: string;
  text: string;
}

/** The in-flight turn's text that isn't stored yet; null while nothing streams. */
export function useChatStream(
  conversationId: string,
  items: readonly ChatItem[] | undefined,
  active: boolean,
): StreamingText | null {
  const dispatcher = useDispatcher();
  const [stream, setStream] = useState<TurnStream | null>(null);
  const itemsRef = useRef(items);

  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  useEffect(
    () =>
      dispatcher.onEnvelope((envelope) => {
        const delta = chatDeltaOf(envelope);
        if (delta?.conversation_id !== conversationId) return;
        setStream((previous) => applyDelta(previous, delta, committedSteps(itemsRef.current ?? [], delta.turn_id)));
      }),
    [dispatcher, conversationId],
  );

  if (!active || stream === null) return null;
  return { turnId: stream.turnId, text: visibleText(stream, committedSteps(items ?? [], stream.turnId)) };
}
