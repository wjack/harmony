/**
 * The chat's queries and mutations over the typed API client.
 *
 * Reads: the conversation (`ChatItem`s) and a request's per-recipient status. Writes
 * start chats, send messages (or send declined ones again), set a chat's send mode,
 * resolve cards and answer inline questions; each refetches what it changed, and the event stream (live.ts) keeps
 * everything current in between.
 */
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { unwrap } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import { useApi } from "../app/context";
import { addOptimistic, removeOptimistic } from "./optimistic";
import type {
  ApproveCard,
  ChatItemOf,
  ConversationDetail,
  ResolveEscalation,
  SendMode,
  StartConversation,
} from "./types";

/** How often to recheck a conversation while Claude works, should a stream event be missed. */
const IN_FLIGHT_RECHECK_MS = 5_000;

export function isWorking(detail: ConversationDetail | undefined): boolean {
  return detail !== undefined && (detail.turn_in_flight || detail.queued_message_ids.length > 0);
}

/**
 * Claude in the chat is looking at news from the request's conversations. It may write
 * an update or nothing, so the chat listens and rechecks but shows no spark.
 */
export function isLooking(detail: ConversationDetail | undefined): boolean {
  return detail?.looking === true;
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
    refetchInterval: (query) =>
      isWorking(query.state.data) || isLooking(query.state.data) ? IN_FLIGHT_RECHECK_MS : false,
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
    mutationFn: (body: StartConversation) => unwrap(api.POST("/api/conversations", { body })),
    onSuccess: (created, { text }) => {
      addOptimistic(created.id, { messageId: created.message_id, text, at: new Date().toISOString(), after: null });
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversations });
    },
  });
}

/**
 * This chat's send mode (LIVE_DEMO_PLAN 3.10): `true` or `false` overrides the
 * organization's default, `null` follows it. The menu shows the choice at once and
 * puts it back if the server refuses it.
 */
export function useSetSendMode(conversationId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const key = queryKeys.conversation(conversationId);
  const setSendMode = (sendMode: SendMode) => {
    queryClient.setQueryData<ConversationDetail>(key, (detail) => detail && { ...detail, send_mode: sendMode });
  };
  return useMutation({
    mutationFn: (override: boolean | null) =>
      unwrap(
        api.PATCH("/api/conversations/{conversation_id}", {
          params: { path: { conversation_id: conversationId } },
          body: { send_without_confirmation: override },
        }),
      ),
    onMutate: async (override) => {
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<ConversationDetail>(key)?.send_mode;
      if (previous !== undefined) {
        setSendMode({ ...previous, override, effective: override ?? previous.org_default });
      }
      return { previous };
    },
    onSuccess: setSendMode,
    onError: (_error, _override, context) => {
      if (context?.previous !== undefined) setSendMode(context.previous);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: key }),
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

/**
 * Send the messages Claude declined to respond to again (`ConversationDetail.refusal`),
 * as they were or as `text`, the member's rewording of them. The server takes them back
 * and queues them for a new turn, so the chat drops them at once and draws what was
 * resent as sent until that turn stores it.
 */
export function useRetryMessage(conversationId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const key = queryKeys.conversation(conversationId);
  return useMutation({
    mutationFn: (text: string | null) =>
      unwrap(
        api.POST("/api/conversations/{conversation_id}/retry", {
          params: { path: { conversation_id: conversationId } },
          body: text === null ? {} : { text },
        }),
      ),
    onSuccess: async ({ message_ids }, text) => {
      // A refetch already on its way would bring the declined messages back.
      await queryClient.cancelQueries({ queryKey: key });
      const shown = queryClient.getQueryData<ConversationDetail>(key);
      const turnId = shown?.refusal?.turn_id;
      const items = shown?.items ?? [];
      const declined = items.filter(
        (item): item is ChatItemOf<"user_text"> => item.type === "user_text" && turnId !== undefined && item.turn_id === turnId,
      );
      const kept = items.filter((item) => !declined.some((message) => message.id === item.id));
      if (shown !== undefined) {
        queryClient.setQueryData<ConversationDetail>(key, { ...shown, items: kept, refusal: null, turn_failure: null });
      }
      const texts = text === null ? declined.map((message) => message.text) : [text];
      const at = new Date().toISOString();
      const after = kept.at(-1)?.id ?? null;
      message_ids.forEach((messageId, index) => {
        const resent = texts[index];
        if (resent !== undefined) addOptimistic(conversationId, { messageId, text: resent, at, after });
      });
      void queryClient.invalidateQueries({ queryKey: key });
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

/** Decline the waiting card: don't send it. A reason, if given, reaches only Claude in this chat. */
export function useRejectCard(conversationId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ cardId, feedback }: { cardId: string; feedback: string | null }) =>
      unwrap(
        api.POST("/api/cards/{card_id}/reject", {
          params: { path: { card_id: cardId } },
          body: feedback === null ? {} : { feedback },
        }),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.conversation(conversationId) }),
  });
}

/**
 * Answer a question routed to the member inline: the same resolution the Requests inbox
 * sends, plus the card's other ticked askers (`also_for`).
 */
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
