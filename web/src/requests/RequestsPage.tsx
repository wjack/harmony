/**
 * `/requests` and `/requests/:threadId` (LIVE_DEMO_PLAN 3.9): the list of requests, or
 * one request full width. The shell mounts this inside its layout; the sidebar lists
 * requests too, so there is no list beside an open one.
 */
import { useParams } from "react-router";

import { RequestView } from "./RequestView";
import { RequestsListPage } from "./RequestsListPage";
import { useRequestsLive } from "./live";

export function RequestsPage() {
  const { threadId } = useParams<{ threadId?: string }>();
  useRequestsLive(threadId);
  return threadId === undefined ? <RequestsListPage /> : <RequestView key={threadId} threadId={threadId} />;
}
