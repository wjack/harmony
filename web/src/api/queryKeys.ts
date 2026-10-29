/**
 * TanStack Query keys, shared by every area so the realtime layer can refresh what a
 * snapshot touches. Entity snapshots from the event stream are cached under
 * `entity(type, id)` as `{ version, data }`; see realtime/cache.ts.
 */
export const queryKeys = {
  session: ["session"] as const,
  members: ["members"] as const,
  threads: ["threads"] as const,
  entity: (entityType: string, entityId: string) => ["entity", entityType, entityId] as const,

  /** `GET /api/conversations`: the sidebar's Recents and the Chats page. */
  conversations: ["conversations"] as const,
  conversation: (conversationId: string) => ["conversation", conversationId] as const,
  /** `GET /api/inbox`: the Requests inbox and the sidebar's Requests badge. */
  inbox: ["inbox"] as const,
  request: (requestId: string) => ["request", requestId] as const,
  thread: (threadId: string) => ["thread", threadId] as const,
  workspace: (threadId: string) => ["workspace", threadId] as const,
  network: (world: string, requestId: string | null) => ["network", world, requestId] as const,
  harmonySettings: (orgSlug: string) => ["harmony-settings", orgSlug] as const,
  /** `GET /api/demo/story`: the suggested message and recipients. */
  story: ["demo-story"] as const,
  /** `GET /api/budget`: whether live models are on, and what stops them now. */
  budget: ["budget"] as const,
  /** `GET /api/demo/waiting`: the accounts with an open item, for the demo bar. */
  demoWaiting: ["demo-waiting"] as const,
};
