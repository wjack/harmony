import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";

import type { ApiClient } from "../api/client";
import type { RealtimeDispatcher } from "../realtime/dispatcher";
import { ApiContext, DispatcherContext } from "./context";

export interface AppServices {
  api: ApiClient;
  queryClient: QueryClient;
  dispatcher: RealtimeDispatcher;
}

export function AppProviders({ api, queryClient, dispatcher, children }: AppServices & { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <ApiContext value={api}>
        <DispatcherContext value={dispatcher}>{children}</DispatcherContext>
      </ApiContext>
    </QueryClientProvider>
  );
}
