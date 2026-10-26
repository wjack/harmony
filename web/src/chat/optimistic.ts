/**
 * Messages the member sent that the conversation doesn't show yet. The workflow stores
 * a message at the start of the turn that drains it, so until then the chat draws it
 * from here, the way Claude shows a message the moment it is sent.
 */
import { useStore } from "zustand";
import { createStore } from "zustand/vanilla";

import type { OptimisticMessage } from "./items";

interface OptimisticState {
  byConversation: Readonly<Record<string, readonly OptimisticMessage[]>>;
}

export const optimisticStore = createStore<OptimisticState>(() => ({ byConversation: {} }));

const NONE: readonly OptimisticMessage[] = [];

export function addOptimistic(conversationId: string, message: OptimisticMessage): void {
  optimisticStore.setState(({ byConversation }) => ({
    byConversation: {
      ...byConversation,
      [conversationId]: [...(byConversation[conversationId] ?? NONE), message],
    },
  }));
}

export function removeOptimistic(conversationId: string, messageIds: readonly string[]): void {
  if (messageIds.length === 0) return;
  optimisticStore.setState(({ byConversation }) => ({
    byConversation: {
      ...byConversation,
      [conversationId]: (byConversation[conversationId] ?? NONE).filter(
        (message) => !messageIds.includes(message.messageId),
      ),
    },
  }));
}

export function useOptimistic(conversationId: string): readonly OptimisticMessage[] {
  return useStore(optimisticStore, (state) => state.byConversation[conversationId] ?? NONE);
}
