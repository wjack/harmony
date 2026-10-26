import type { Inbox, InboxItem } from "../api/types";

/** The inbox item for `escalationId`, in any of the inbox's groups. */
export function itemForEscalation(inbox: Inbox, escalationId: string): InboxItem | null {
  for (const group of [inbox.needs_you, inbox.with_your_team, inbox.claude_handling, inbox.shared_with_you, inbox.done]) {
    const item = group.find((candidate) => candidate.escalation?.id === escalationId);
    if (item !== undefined) return item;
  }
  return null;
}

/**
 * The chat an item's question is answered in: a question the member's own side's
 * representative asked them shows inline in the chat that sent the request (§7), and
 * the inbox only names that chat when it is theirs.
 */
export function answeringChat(item: InboxItem): string | null {
  if (item.side !== "requester" || item.escalation?.kind !== "consult") return null;
  return item.conversation_id;
}

/** Where an item opens: the chat it is answered in, else its thread in Requests. */
export function escalationPath(item: InboxItem): string {
  const chat = answeringChat(item);
  return chat === null ? `/requests/${encodeURIComponent(item.thread_id)}` : `/chat/${encodeURIComponent(chat)}`;
}
