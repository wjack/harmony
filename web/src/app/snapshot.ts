import type { QueryClient } from "@tanstack/react-query";

import { ApiError, readCursor, type ApiClient } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import type { StreamSnapshot } from "../realtime/dispatcher";

/**
 * The dispatcher's snapshot: `GET /api/session` and its cursor, or null without a
 * session (401). The session goes into the query cache when the dispatcher adopts the
 * snapshot (`prime`), so the UI shows the acting member without fetching it again, and
 * a switch never shows the new member inside the previous member's frame.
 */
export async function fetchStreamSnapshot(
  api: ApiClient,
  queryClient: QueryClient,
): Promise<StreamSnapshot | null> {
  const { data, error, response } = await api.GET("/api/session");
  if (response.status === 401) return null;
  if (!response.ok || data === undefined) throw ApiError.from(response, error);
  return {
    cursor: readCursor(response),
    epoch: data.epoch,
    sessionId: data.session_id,
    prime: () => {
      queryClient.setQueryData(queryKeys.session, data);
    },
  };
}
