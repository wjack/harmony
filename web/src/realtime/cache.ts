import type { QueryClient, QueryKey } from "@tanstack/react-query";

import { queryKeys } from "../api/queryKeys";
import type { EntitySnapshot } from "../api/types";

/** What the cache holds under `queryKeys.entity(type, id)`. */
export interface Versioned<T> {
  version: number;
  data: T;
}

export type EntityData = EntitySnapshot["data"];

/** List queries that show an entity type, refreshed when one of its entities changes. */
const LIST_KEYS_BY_ENTITY_TYPE: Readonly<Partial<Record<string, readonly QueryKey[]>>> = {
  thread: [queryKeys.threads, queryKeys.inbox],
  // Recents order by last activity and flag a pending card.
  conversation: [queryKeys.conversations],
  card: [queryKeys.conversations],
  // The Requests badge counts open items that need the acting member.
  escalation: [queryKeys.inbox],
};

/**
 * Cache an entity snapshot only if it is newer than the cached version.
 *
 * Events carry full snapshots, not diffs, so the newest version wins outright. A REST
 * response fetched after the stream's cursor can already hold a later version than an
 * event still in flight; the version gate keeps that event from rolling it back.
 * Returns whether the snapshot was applied.
 */
export function applyEntitySnapshot(queryClient: QueryClient, snapshot: EntitySnapshot): boolean {
  const key = queryKeys.entity(snapshot.entity_type, snapshot.entity_id);
  const cached = queryClient.getQueryData<Versioned<EntityData>>(key);
  if (cached !== undefined && cached.version >= snapshot.version) return false;

  queryClient.setQueryData<Versioned<EntityData>>(key, {
    version: snapshot.version,
    data: snapshot.data,
  });
  for (const listKey of LIST_KEYS_BY_ENTITY_TYPE[snapshot.entity_type] ?? []) {
    void queryClient.invalidateQueries({ queryKey: listKey });
  }
  return true;
}
