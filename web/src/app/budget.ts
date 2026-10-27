/**
 * The visitor's live-model budget (`GET /api/budget`, LIVE_DEMO_PLAN 3.5): whether live
 * models are on, and which cap, if any, stops live turns now.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { unwrap } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import type { components } from "../api/schema";
import { useApi, useDispatcher, useRealtime } from "./context";

export type BudgetView = components["schemas"]["BudgetView"];
export type BudgetState = BudgetView["state"];

/**
 * Spend moves without an entity to snapshot, so the budget is polled: every 30 s while
 * the tab is visible (TanStack pauses intervals in the background), and at once when a
 * call is refused (`demo.notice {kind: "budget"}`).
 */
const POLL_MS = 30_000;

export function useBudget() {
  const api = useApi();
  const dispatcher = useDispatcher();
  const queryClient = useQueryClient();
  // The budget is the session's lineage: there is none to read before a session starts.
  const hasSession = useRealtime((state) => state.sessionId !== null && state.status !== "locked");

  useEffect(
    () =>
      dispatcher.onDemo((event) => {
        if (event.type === "demo.notice" && event.kind === "budget") {
          void queryClient.invalidateQueries({ queryKey: queryKeys.budget }, { cancelRefetch: false });
        }
      }),
    [dispatcher, queryClient],
  );

  return useQuery({
    queryKey: queryKeys.budget,
    queryFn: () => unwrap(api.GET("/api/budget")),
    enabled: hasSession,
    refetchInterval: POLL_MS,
  });
}
