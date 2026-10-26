import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "./client";

const MAX_RETRIES = 2;

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // The event stream keeps cached data current; refetching on focus would only
        // repeat what the dispatcher already applied.
        refetchOnWindowFocus: false,
        staleTime: 30_000,
        // A 4xx (no session, no access, 404 for a hidden thread) won't change on retry.
        retry: (failureCount, error) =>
          !(error instanceof ApiError && error.status < 500) && failureCount < MAX_RETRIES,
      },
    },
  });
}
