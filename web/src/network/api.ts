/**
 * The network view's reads and writes over the typed API client.
 *
 * `GET /api/network` is read with its `X-Harmony-Cursor`: the lens stream (lens.ts)
 * opens from that cursor, so no event falls between the snapshot and the stream.
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { ApiError, readCursor, unwrap } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import { useApi } from "../app/context";
import { allMembers } from "../demo/cast";
import type { NetworkView, WorldLens } from "./types";

export const networkKeys = {
  /** Every focused request's view in `world` (the prefix of `queryKeys.network`). */
  world: (world: WorldLens) => ["network", world] as const,
  session: (world: WorldLens) => ["network-session", world] as const,
  members: (world: WorldLens) => ["network-members", world] as const,
};

export interface NetworkSnapshot {
  view: NetworkView;
  /** The world seq the view is consistent with; null if the response lacked it. */
  cursor: number | null;
}

/** The graph for `world`, focused on `requestId` (or the latest visible request). */
export function useNetworkView(world: WorldLens, requestId: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.network(world, requestId),
    queryFn: async (): Promise<NetworkSnapshot> => {
      const { data, error, response } = await api.GET("/api/network", {
        params: { query: requestId === null ? { world } : { world, request: requestId } },
      });
      if (!response.ok || data === undefined) throw ApiError.from(response, error);
      return { view: data, cursor: readCursor(response) };
    },
    // Switching the focused request keeps the canvas up while the next one loads;
    // switching worlds does not, since none of the old world's orgs carry over.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === world ? keepPreviousData(previous) : undefined,
    staleTime: 10_000,
  });
}

/**
 * The session as seen from `world`: its epoch (the lens stream's `?epoch=`) and the
 * world's mode for the header badge. A 401 means the session has no such world yet.
 */
export function useLensSession(world: WorldLens) {
  const api = useApi();
  return useQuery({
    queryKey: networkKeys.session(world),
    queryFn: () => unwrap(api.GET("/api/session", { params: { query: { world } } })),
    staleTime: 60_000,
  });
}

/**
 * The members of `world` (each world has its own rows), to tell which org a waiting
 * member belongs to. The account switcher's directory is the desk world's only.
 */
export function useWorldMembers(world: WorldLens, enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: networkKeys.members(world),
    queryFn: () => unwrap(api.GET("/api/members", { params: { query: { world } } })),
    select: allMembers,
    enabled,
    staleTime: 60_000,
  });
}

/**
 * The focused request's origin chat, where a requester-side question waits inline. Read
 * only when such a question is shown; null if the actor can't see the request.
 */
export function useRequestConversation(requestId: string | null, enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.request(requestId ?? ""),
    queryFn: () =>
      unwrap(api.GET("/api/requests/{request_id}", { params: { path: { request_id: requestId ?? "" } } })),
    select: (request) => request.conversation_id,
    enabled: enabled && requestId !== null,
    retry: false,
  });
}

/**
 * Whether a failed read means the session has no such world yet (the API's own 401:
 * start it). The passcode gate's 401 means the visitor is locked out, not that the
 * world is missing.
 */
export function isMissingWorld(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401 && error.code !== "passcode_required";
}
