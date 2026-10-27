/**
 * Turns a conversation's `ChatItem`s into what the transcript draws: tool activity and
 * thinking from one turn collapse into one "activity" line (Claude's "Used Slack
 * integration ›"), a card a newer draft replaced folds to one "earlier version" row
 * under the message that asked for it, and each reply ends with the copy / feedback row.
 */
import type { CommittedStep } from "./stream";
import type {
  AssistantTextItem,
  ChatItem,
  ConversationDetail,
  InlineQuestionItem,
  OutreachCardItem,
  RequestStatusItem,
  ThinkingSummaryItem,
  ToolActivityItem,
  UserTextItem,
} from "./types";

export type ActivityItem = ToolActivityItem | ThinkingSummaryItem;

export type Block =
  | { kind: "user"; key: string; item: UserTextItem; pending: boolean }
  | { kind: "assistant"; key: string; item: AssistantTextItem; endOfReply: boolean }
  | { kind: "activity"; key: string; items: ActivityItem[] }
  | { kind: "outreach"; key: string; item: OutreachCardItem }
  | { kind: "earlier"; key: string; item: OutreachCardItem }
  | { kind: "request"; key: string; item: RequestStatusItem }
  | { kind: "question"; key: string; item: InlineQuestionItem };

function isActivity(item: ChatItem): item is ActivityItem {
  return item.type === "tool_activity" || item.type === "thinking_summary";
}

export function toBlocks(items: readonly ChatItem[]): Block[] {
  const blocks: Block[] = [];
  for (const item of items) {
    if (isActivity(item)) {
      const last = blocks.at(-1);
      // Consecutive steps of one turn read as one line; a new turn starts a new one.
      if (last?.kind === "activity" && last.items[0]?.turn_id === item.turn_id) {
        last.items.push(item);
      } else {
        blocks.push({ kind: "activity", key: item.id, items: [item] });
      }
      continue;
    }
    switch (item.type) {
      case "user_text":
        blocks.push({ kind: "user", key: item.id, item, pending: false });
        break;
      case "assistant_text":
        blocks.push({ kind: "assistant", key: item.id, item, endOfReply: false });
        break;
      case "outreach_card":
        if (wasReplaced(item)) {
          blocks.push({ kind: "earlier", key: item.id, item });
          // The message that turned it down is stored as its feedback, not as a message.
          if (item.status === "rejected" && item.feedback !== null) {
            blocks.push({ kind: "user", key: `${item.id}:message`, item: messageOf(item, item.feedback), pending: false });
          }
        } else {
          blocks.push({ kind: "outreach", key: item.id, item });
        }
        break;
      case "request_status":
        blocks.push({ kind: "request", key: item.id, item });
        break;
      case "inline_question":
        blocks.push({ kind: "question", key: item.id, item });
        break;
    }
  }
  markEndsOfReplies(blocks);
  return blocks;
}

/**
 * Whether a card gave way to a newer draft: Claude proposed another in its place, or a
 * message typed while it waited asked for one. A Decline isn't: it means don't send.
 */
export function wasReplaced(card: OutreachCardItem): boolean {
  return card.status === "superseded" || card.rejected_with === "message";
}

function messageOf(card: OutreachCardItem, text: string): UserTextItem {
  return { id: `${card.id}:message`, at: card.at, type: "user_text", text, member_id: null };
}

/** A reply runs from one member message to the next; its last text gets the action row. */
function markEndsOfReplies(blocks: Block[]): void {
  let lastText: Extract<Block, { kind: "assistant" }> | null = null;
  for (const block of blocks) {
    if (block.kind === "user") {
      if (lastText !== null) lastText.endOfReply = true;
      lastText = null;
    } else if (block.kind === "assistant") {
      lastText = block;
    }
  }
  if (lastText !== null) lastText.endOfReply = true;
}

/**
 * The outreach card waiting on the member, drawn in the transcript; null when none is.
 * A card the chat sends without review is pending only while it goes out: nobody
 * answers it (LIVE_DEMO_PLAN 3.10).
 */
export function pendingCard(detail: Pick<ConversationDetail, "items" | "pending_card_id">): OutreachCardItem | null {
  for (const item of detail.items) {
    if (item.type !== "outreach_card" || item.status !== "pending" || item.auto) continue;
    if (detail.pending_card_id === null || item.card_id === detail.pending_card_id) return item;
  }
  return null;
}

/** One turn's committed text blocks, in order: what its streamed deltas became. */
export function committedSteps(items: readonly ChatItem[], turnId: string): CommittedStep[] {
  return items
    .filter((item): item is AssistantTextItem => item.type === "assistant_text" && item.turn_id === turnId)
    .map((item) => ({ id: item.id, markdown: item.markdown }));
}

/** A message the member sent that the conversation doesn't show yet. */
export interface OptimisticMessage {
  messageId: string;
  text: string;
  at: string;
  /** The conversation's last item when the message was sent; only later items can settle it. */
  after: string | null;
}

/**
 * The messages in `messages` the conversation now shows. The turn that drains queued
 * messages stores them together, as one `user` row named by the turn, so a message
 * shows as a member text with its words that came after it was sent. A message drained
 * while a card waits answers the card instead (§12): a card rejected after the message
 * was sent, with the message as its feedback. Each item settles at most one message.
 */
export function settledMessages(items: readonly ChatItem[], messages: readonly OptimisticMessage[]): Set<string> {
  const settled = new Set<string>();
  const claimed = new Set<string>();
  for (const message of messages) {
    const start = message.after === null ? 0 : items.findIndex((item) => item.id === message.after) + 1;
    const shown = items
      .slice(start)
      .find(
        (item) =>
          !claimed.has(item.id) &&
          ((item.type === "user_text" && item.text === message.text) ||
            (item.type === "outreach_card" && item.status === "rejected" && item.feedback === message.text)),
      );
    if (shown !== undefined) {
      claimed.add(shown.id);
      settled.add(message.messageId);
    }
  }
  return settled;
}

export function optimisticBlock(message: OptimisticMessage): Block {
  return {
    kind: "user",
    key: `optimistic:${message.messageId}`,
    pending: true,
    item: { id: message.messageId, at: message.at, type: "user_text", text: message.text, member_id: null },
  };
}
