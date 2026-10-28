/**
 * The Requests inbox's queries and mutations, over the typed API client.
 *
 * Reads: P0's `GET /api/threads/{id}` (the transcript both sides see) and
 * `GET /api/threads/{id}/workspace` (our side only); the inbox itself is the shell's
 * `useInbox`, which the sidebar badge shares. Writes go through the escalation, notes,
 * steers, reopen and share endpoints, then refetch what they changed; the event stream
 * (see live.ts) keeps everything current in between.
 */
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { ApiError, unwrap } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import { useApi } from "../app/context";
import { useSession } from "../app/queries";
import type { AddSteer, ReopenRequest, ReopenStarted, SteerStarted } from "../api/types";
import type {
  AddNote,
  AnswerUsed,
  ApiProblem,
  EscalationResolved,
  MemberView,
  NoteView,
  Reassigned,
  ReassignEscalation,
  ResolveEscalation,
  RetryEscalation,
  ShareRequest,
  ShareResult,
} from "./types";

const PROBLEM_CODES: ReadonlySet<string> = new Set<ApiProblem["code"]>([
  "not_found",
  "invalid",
  "not_admin",
  "card_not_pending",
  "escalation_not_open",
  "not_in_org",
  "world_retired",
  "member_changed",
  "steer_running",
  "thread_closed",
  "thread_open",
  "reopen_in_progress",
  "closing_in_progress",
  "not_reopenable",
]);

/** The route's typed `ApiProblem` (with `resolved_by`) when the failure carried one. */
export function problemOf(error: unknown): ApiProblem | null {
  if (!(error instanceof ApiError) || error.problem === null) return null;
  return PROBLEM_CODES.has(error.problem.code) ? (error.problem as ApiProblem) : null;
}

/** How long an Edit or a reopen waits for Harmony before the member may try again. */
export const WRITE_TIMEOUT_MS = 20_000;

/** The request never got an answer: fetch rejected, or `WRITE_TIMEOUT_MS` passed. */
export class NoResponse extends Error {
  constructor() {
    super("Harmony didn't respond.");
    this.name = "NoResponse";
  }
}

/**
 * `request`, failing with `NoResponse` when it gets no answer in time or fetch rejects.
 * The request itself may still land; its `client_key` makes the retry the same write.
 */
async function answered<T>(request: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(new NoResponse());
    }, WRITE_TIMEOUT_MS);
  });
  try {
    return await Promise.race([request, timeout]);
  } catch (error) {
    throw error instanceof ApiError ? error : new NoResponse();
  } finally {
    clearTimeout(timer);
  }
}

// --- Reads -------------------------------------------------------------------------------

/** The shared transcript and who can see the thread. */
export function useThreadDetail(threadId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.thread(threadId ?? ""),
    queryFn: () =>
      unwrap(api.GET("/api/threads/{thread_id}", { params: { path: { thread_id: threadId ?? "" } } })),
    enabled: threadId !== undefined,
  });
}

/** Our side of the thread: consults, escalations with drafts, notes, what our representative saw. */
export function useWorkspace(threadId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.workspace(threadId ?? ""),
    queryFn: () =>
      unwrap(
        api.GET("/api/threads/{thread_id}/workspace", { params: { path: { thread_id: threadId ?? "" } } }),
      ),
    enabled: threadId !== undefined,
  });
}

/** Members of `orgId`, for Reassign and Share. */
export function useOrgMembers(orgId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.members,
    queryFn: () => unwrap(api.GET("/api/members")),
    enabled: orgId !== undefined,
    select: (directory): MemberView[] => {
      const seen = new Set<string>();
      return [...directory.featured, ...directory.all].filter((member) => {
        if (member.org.id !== orgId || seen.has(member.id)) return false;
        seen.add(member.id);
        return true;
      });
    },
  });
}

/**
 * Whether the viewer has lost `threadId`: its last fetch came back not found, as after a
 * reassign or unshare that took away their only access.
 */
export function lostThread(queryClient: QueryClient, threadId: string): boolean {
  const error = queryClient.getQueryState(queryKeys.thread(threadId))?.error;
  return error instanceof ApiError && error.status === 404;
}

// --- Writes ------------------------------------------------------------------------------

function refreshThread(queryClient: QueryClient, threadId: string): Promise<unknown> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.inbox }),
    queryClient.invalidateQueries({ queryKey: queryKeys.thread(threadId) }),
    queryClient.invalidateQueries({ queryKey: queryKeys.workspace(threadId) }),
  ]);
}

export interface EscalationTarget {
  threadId: string;
  escalationId: string;
}

export function useResolveEscalation({ threadId, escalationId }: EscalationTarget) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<EscalationResolved, Error, ResolveEscalation>({
    mutationFn: (body) =>
      unwrap(
        api.POST("/api/escalations/{escalation_id}/resolve", {
          params: { path: { escalation_id: escalationId } },
          body,
        }),
      ),
    onSettled: () => refreshThread(queryClient, threadId),
  });
}

/**
 * Run the step Claude declined on a question again (`EscalationView.refusal`), as the
 * item's next draft rev; with `draft_text`, the check runs on the member's edit of the draft.
 */
export function useRetryEscalation({ threadId, escalationId }: EscalationTarget) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<AnswerUsed, Error, RetryEscalation>({
    mutationFn: (body) =>
      unwrap(
        api.POST("/api/escalations/{escalation_id}/retry", {
          params: { path: { escalation_id: escalationId } },
          body,
        }),
      ),
    onSettled: () => refreshThread(queryClient, threadId),
  });
}

export function useReassignEscalation({ threadId, escalationId }: EscalationTarget) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<Reassigned, Error, ReassignEscalation>({
    mutationFn: (body) =>
      unwrap(
        api.POST("/api/escalations/{escalation_id}/reassign", {
          params: { path: { escalation_id: escalationId } },
          body,
        }),
      ),
    onSettled: () => refreshThread(queryClient, threadId),
  });
}

/** Adds a note as the member this tab shows, so a switch in another tab is caught. */
export function useAddNote(threadId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const memberId = useSession().data?.member.id;
  return useMutation<NoteView, Error, Omit<AddNote, "as_member_id">>({
    mutationFn: ({ body }) => {
      if (memberId === undefined) throw new Error("No session yet.");
      return unwrap(
        api.POST("/api/threads/{thread_id}/notes", {
          params: { path: { thread_id: threadId } },
          body: { body, as_member_id: memberId },
        }),
      );
    },
    onSettled: () => refreshThread(queryClient, threadId),
  });
}

/** An Edit: the member's instruction to our Claude, sent as the member this tab shows (EDIT.md §8.3). */
export function useSteer(threadId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const memberId = useSession().data?.member.id;
  return useMutation<SteerStarted, Error, Omit<AddSteer, "as_member_id">>({
    mutationFn: (body) => {
      if (memberId === undefined) throw new Error("No session yet.");
      return answered(
        unwrap(
          api.POST("/api/threads/{thread_id}/steers", {
            params: { path: { thread_id: threadId } },
            body: { ...body, as_member_id: memberId },
          }),
        ),
      );
    },
    onSettled: () => refreshThread(queryClient, threadId),
  });
}

/** Reopens a closed conversation with what the member typed (REOPEN.md §4.2). */
export function useReopenThread(threadId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const memberId = useSession().data?.member.id;
  return useMutation<ReopenStarted, Error, Omit<ReopenRequest, "as_member_id">>({
    mutationFn: (body) => {
      if (memberId === undefined) throw new Error("No session yet.");
      return answered(
        unwrap(
          api.POST("/api/threads/{thread_id}/reopen", {
            params: { path: { thread_id: threadId } },
            body: { ...body, as_member_id: memberId },
          }),
        ),
      );
    },
    onSettled: () => refreshThread(queryClient, threadId),
  });
}

export function useShareThread(threadId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ShareResult, Error, ShareRequest>({
    mutationFn: (body) =>
      unwrap(api.POST("/api/threads/{thread_id}/share", { params: { path: { thread_id: threadId } }, body })),
    onSettled: () => refreshThread(queryClient, threadId),
  });
}
