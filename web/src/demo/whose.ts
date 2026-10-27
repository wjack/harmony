import { useQuery } from "@tanstack/react-query";

import { unwrap } from "../api/client";
import type { DemoWhose } from "../api/types";
import { useApi } from "../app/context";

export type WhoseKind = DemoWhose["kind"];

/**
 * `GET /api/demo/whose`: who in this demo can open a chat or request the acting member
 * can't, by name and org only (demo chrome, ARCHITECTURE.md §11.4). A 404 means the id
 * isn't in this demo's world at all: an earlier visit's, or from before a reset.
 */
export function useWhose(kind: WhoseKind, id: string) {
  const api = useApi();
  return useQuery({
    queryKey: ["demo-whose", kind, id] as const,
    queryFn: () =>
      unwrap(api.GET("/api/demo/whose", { params: { query: kind === "chat" ? { chat: id } : { thread: id } } })),
  });
}
