/**
 * The chat's queries and mutations over the typed API client.
 *
 * Reads: the conversation (`ChatItem`s) and a request's per-recipient status. Writes
 * start chats, send messages, resolve cards and answer inline questions; each refetches
 * what it changed, and the event stream (live.ts) keeps everything current in between.
 */
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { unwrap } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import { useApi } from "../app/context";
import { addOptimistic, removeOptimistic } from "./optimistic";
import type { ApproveCard, ConversationDetail, ResolveEscalation } from "./types";

/** How often to recheck a conversation while Claude works, should a stream event be missed. */
const IN_FLIGHT_RECHECK_MS = 5_000;

export function isWorking(detail: ConversationDetail | undefined): boolean {
  return detail !== undefined && (detail.turn_in_flight || detail.queued_message_ids.length > 0);
}

export function useConversation(conversationId: string) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.conversation(conversationId),
    queryFn: () =>
      unwrap(
        api.GET("/api/conversations/{conversation_id}", {
          params: { path: { conversation_id: conversationId } },
        }),
      ),
    refetchInterval: (query) => (isWorking(query.state.data) ? IN_FLIGHT_RECHECK_MS : false),
  });
}

function requestStatusQuery(api: ReturnType<typeof useApi>, requestId: string) {
  return {
    queryKey: queryKeys.request(requestId),
    queryFn: () =>
      unwrap(api.GET("/api/requests/{request_id}", { params: { path: { request_id: requestId } } })),
  };
}

export function useRequestStatus(requestId: string) {
  const api = useApi();
  return useQuery(requestStatusQuery(api, requestId));
}

/**
 * Whether any of these network requests is still active (or not loaded yet). It reads
 * the status cards' own queries, so it costs no extra fetch.
 */
export function useAnyRequestActive(requestIds: readonly string[]): boolean {
  const api = useApi();
  return useQueries({
    queries: requestIds.map((requestId) => requestStatusQuery(api, requestId)),
    combine: (results) => results.some((result) => result.data === undefined || result.data.status === "active"),
  });
}

export function useStartConversation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (text: string) => unwrap(api.POST("/api/conversations", { body: { text } })),
    onSuccess: (created, text) => {
      addOptimistic(created.id, { messageId: created.message_id, text, at: new Date().toISOString(), after: null });
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversations });
    },
  });
}

/** A member message. While a card is pending, the server takes it as the card's rejection feedback. */
export function usePostMessage(conversationId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (text: string) =>
      unwrap(
        api.POST("/api/conversations/{conversation_id}/messages", {
          params: { path: { conversation_id: conversationId } },
          body: { text },
        }),
      ),
    onSuccess: (posted, text) => {
      // Feedback on a card shows on the card, not as a message.
      if (posted.outcome === "queued") {
        const shown = queryClient.getQueryData<ConversationDetail>(queryKeys.conversation(conversationId));
        addOptimistic(conversationId, {
          messageId: posted.message_id,
          text,
          at: new Date().toISOString(),
          after: shown?.items.at(-1)?.id ?? null,
        });
      } else {
        removeOptimistic(conversationId, [posted.message_id]);
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversation(conversationId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversations });
    },
  });
}

export function useApproveCard(conversationId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ cardId, body }: { cardId: string; body: ApproveCard }) =>
      unwrap(api.POST("/api/cards/{card_id}/approve", { params: { path: { card_id: cardId } }, body })),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.conversation(conversationId) }),
  });
}

export function useRejectCard(conversationId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ cardId, feedback }: { cardId: string; feedback: string }) =>
      unwrap(
        api.POST("/api/cards/{card_id}/reject", { params: { path: { card_id: cardId } }, body: { feedback } }),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.conversation(conversationId) }),
  });
}

/** Answer a question routed to the member inline: the same resolution the Requests inbox sends. */
export function useResolveQuestion(conversationId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ escalationId, body }: { escalationId: string; body: ResolveEscalation }) =>
      unwrap(
        api.POST("/api/escalations/{escalation_id}/resolve", {
          params: { path: { escalation_id: escalationId } },
          body,
        }),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.conversation(conversationId) }),
  });
}
