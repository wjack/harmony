import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";

import type { ApiClient } from "../api/client";
import type { RealtimeDispatcher } from "../realtime/dispatcher";
import { ApiContext, DispatcherContext } from "./context";
import { useGateLock } from "./lock";

export interface AppServices {
  api: ApiClient;
  queryClient: QueryClient;
  dispatcher: RealtimeDispatcher;
}

export function AppProviders({ api, queryClient, dispatcher, children }: AppServices & { children: ReactNode }) {
  useGateLock(api, dispatcher);
  return (
    <QueryClientProvider client={queryClient}>
      <ApiContext value={api}>
        <DispatcherContext value={dispatcher}>{children}</DispatcherContext>
      </ApiContext>
    </QueryClientProvider>
  );
}
