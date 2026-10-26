import { createContext, use } from "react";
import { useStore } from "zustand";

import type { ApiClient } from "../api/client";
import type { RealtimeDispatcher, RealtimeState } from "../realtime/dispatcher";

export const ApiContext = createContext<ApiClient | null>(null);
export const DispatcherContext = createContext<RealtimeDispatcher | null>(null);

export function useApi(): ApiClient {
  const api = use(ApiContext);
  if (api === null) throw new Error("useApi must be used inside <AppProviders>");
  return api;
}

export function useDispatcher(): RealtimeDispatcher {
  const dispatcher = use(DispatcherContext);
  if (dispatcher === null) throw new Error("useDispatcher must be used inside <AppProviders>");
  return dispatcher;
}

export function useRealtime<T>(selector: (state: RealtimeState) => T): T {
  return useStore(useDispatcher().store, selector);
}
