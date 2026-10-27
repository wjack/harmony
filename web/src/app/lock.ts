/**
 * The passcode gate from the SPA's side (LIVE_DEMO_PLAN 3.2). Any request the gate
 * refuses locks the tab, and "Lock Harmony" locks it on purpose; either way the
 * dispatcher does the locking (`RealtimeDispatcher.lock`).
 */
import { useMutation } from "@tanstack/react-query";
import { useEffect } from "react";

import { ApiError, describeError, lockOnPasscodeRequired, type ApiClient } from "../api/client";
import { clearAllAlerts } from "../demo/alerts";
import type { RealtimeDispatcher } from "../realtime/dispatcher";
import { dismissAllToasts, toast } from "../shell/toasts";
import { useApi, useDispatcher } from "./context";

/**
 * Lock on the gate's 401 from any request, mutations included, and drop the state kept
 * outside the query cache as the tab locks: alerts and toasts name the cast.
 */
export function useGateLock(api: ApiClient, dispatcher: RealtimeDispatcher): void {
  useEffect(() => {
    const middleware = lockOnPasscodeRequired(() => {
      dispatcher.lock();
    });
    api.use(middleware);
    const unsubscribe = dispatcher.onLocked(() => {
      clearAllAlerts();
      dismissAllToasts();
    });
    return () => {
      api.eject(middleware);
      unsubscribe();
    };
  }, [api, dispatcher]);
}

/** The account menu's "Lock Harmony": forget the gate cookie, then lock every tab. */
export function useLockHarmony() {
  const api = useApi();
  const dispatcher = useDispatcher();
  return useMutation({
    mutationFn: async () => {
      const { response, error } = await api.POST("/api/gate/lock");
      // A 401 is the gate's own: already locked (another tab, a rotated passcode).
      if (!response.ok && response.status !== 401) throw ApiError.from(response, error);
    },
    onSuccess: () => {
      dispatcher.lock();
    },
    onError: (error) => {
      toast({ text: `Couldn't lock Harmony: ${describeError(error)}`, key: "lock" });
    },
  });
}
