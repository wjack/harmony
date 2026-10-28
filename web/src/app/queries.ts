import { useMutation, useQuery } from "@tanstack/react-query";

import { unwrap } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import { useApi, useDispatcher } from "./context";

/** The acting session. The dispatcher's snapshot fills it, so it never goes stale. */
export function useSession() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.session,
    queryFn: () => unwrap(api.GET("/api/session")),
    staleTime: Infinity,
  });
}

export function useMembers() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.members,
    queryFn: () => unwrap(api.GET("/api/members")),
  });
}

/** The acting member's inbox. */
export function useThreads() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.threads,
    queryFn: () => unwrap(api.GET("/api/threads")),
  });
}

/** The acting member's chats, most recent first: the sidebar's Recents and /chats. */
export function useConversations() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.conversations,
    queryFn: () => unwrap(api.GET("/api/conversations")),
  });
}

/** The acting member's Requests inbox (its `needs_you` length is the sidebar badge). */
export function useInbox() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.inbox,
    queryFn: () => unwrap(api.GET("/api/inbox")),
  });
}

/** The walkthrough's story beat: suggested message and recipients, and the world date. */
export function useDemoStory() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.story,
    queryFn: () => unwrap(api.GET("/api/demo/story")),
    staleTime: 5 * 60_000,
  });
}

/** Create the visitor's session (and its desk world), then open the event stream. */
export function useStartSession() {
  const api = useApi();
  const dispatcher = useDispatcher();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/api/session/start")),
    onSuccess: () => {
      void dispatcher.start();
    },
  });
}

/** Act as another member of the session's world, then reset this tab's stream and cache. */
export function useActAs() {
  const api = useApi();
  const dispatcher = useDispatcher();
  return useMutation({
    mutationFn: (memberId: string) =>
      unwrap(api.POST("/api/session/act-as", { body: { member_id: memberId } })),
    onSuccess: (session) => {
      void dispatcher.memberChanged(session.epoch, { sessionId: session.session_id, memberId: session.member.id });
    },
  });
}
