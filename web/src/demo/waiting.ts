/**
 * The accounts with an open item (`GET /api/demo/waiting`), for the badge on the demo
 * bar's Network view: how many accounts besides the acting one need a human. Items open
 * and close on other accounts without a cue to this one, so it is polled, and read again
 * at once on `demo.needs_human`.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { unwrap } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import { useApi, useDispatcher, useRealtime } from "../app/context";

const POLL_MS = 10_000;

/** How many accounts other than `actingId` have an open item. */
export function useOthersWaiting(actingId: string | null): number {
  const api = useApi();
  const dispatcher = useDispatcher();
  const queryClient = useQueryClient();
  const hasSession = useRealtime((state) => state.sessionId !== null && state.status !== "locked");

  useEffect(
    () =>
      dispatcher.onDemo((event) => {
        if (event.type === "demo.needs_human") {
          void queryClient.invalidateQueries({ queryKey: queryKeys.demoWaiting }, { cancelRefetch: false });
        }
      }),
    [dispatcher, queryClient],
  );

  const waiting = useQuery({
    queryKey: queryKeys.demoWaiting,
    queryFn: () => unwrap(api.GET("/api/demo/waiting")),
    enabled: hasSession,
    refetchInterval: POLL_MS,
  }).data;
  return waiting?.accounts.filter((account) => account.member.id !== actingId).length ?? 0;
}
