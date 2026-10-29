/**
 * Whether live models can run now (`GET /api/budget`, LIVE_DEMO_PLAN 3.5). `live` says
 * whether the server runs them at all, and `state` what, if anything, stops live turns:
 * the operator's pause, or the demo's budget (the provider refused a call on its spend
 * limit, so live calls wait 10 minutes; locally, the dev ledger ran out).
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
 * The state moves without an entity to snapshot, so it is polled: every 30 s while the
 * tab is visible (TanStack pauses intervals in the background), and at once on
 * `demo.notice {kind: "budget"}`, which comes when a call is refused and when the
 * operator pauses or resumes live models.
 */
const POLL_MS = 30_000;

export function useBudget() {
  const api = useApi();
  const dispatcher = useDispatcher();
  const queryClient = useQueryClient();
  // The route needs a session: there is nothing to read before one starts.
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
