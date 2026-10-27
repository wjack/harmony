import type { ReactNode } from "react";

import { cx } from "../shell/ui/cx";
import { InlineQuestionCard } from "./cards/InlineQuestionCard";
import { EarlierVersion, OutreachRecord } from "./cards/OutreachCard";
import { RequestStatusCard } from "./cards/RequestStatusCard";
import type { Block } from "./items";
import type { StreamingText } from "./live";
import { ActivityLine, AssistantMessage, StreamingSpark, UserMessage } from "./Messages";
import { Markdown } from "./Markdown";

export interface TranscriptActions {
  /** Answering inline questions: which one is sending, and failures by escalation id. */
  answering: string | null;
  answerErrors: Readonly<Record<string, string>>;
  /** Why live turns can't start now (`budgetLine`): answers hold until it clears. */
  blocked: string | null;
  onAnswer: (escalationId: string, text: string) => void;
  onUnavailable: (feature: string) => void;
}

export interface TranscriptProps extends TranscriptActions {
  blocks: readonly Block[];
  memberId: string | null;
  /** Claude is working: no reply is finished yet, so the last one keeps its actions hidden. */
  working: boolean;
  /** Draw the spark: a turn is running (not paused on a card). */
  showSpark: boolean;
  /** Why Claude's last turn failed (failureCopy.ts), while nothing has run since; else null. */
  failure: string | null;
  streaming: StreamingText | null;
  /** The outreach card waiting on the member, drawn by the chat in its block's place. */
  pending: { cardId: string; node: ReactNode } | null;
  /** Cards the member approved with wording of their own, by card id. */
  editedCards: ReadonlySet<string>;
}

/** Cards sit a little outside the text column, as Claude's tool results do. */
const CARD_KINDS: ReadonlySet<Block["kind"]> = new Set(["outreach", "request", "question"]);

/** Claude's rhythm: a wide gap between a member message and the reply, tight within it. */
function spacing(block: Block, previous: Block | undefined): string {
  if (previous === undefined) return "";
  if (block.kind === "user") return "mt-8";
  if (previous.kind === "user") return "mt-12";
  return "mt-4";
}

/** When each request went out, by request id: its status card's time. */
function sentTimes(blocks: readonly Block[]): Map<string, string> {
  return new Map(blocks.flatMap((block) => (block.kind === "request" ? [[block.item.request_id, block.item.at]] : [])));
}

/** Each reply's text, for its Copy button: everything Claude wrote since the last member message. */
function replyTexts(blocks: readonly Block[]): Map<string, string> {
  const texts = new Map<string, string>();
  let current: string[] = [];
  for (const block of blocks) {
    if (block.kind === "user") current = [];
    if (block.kind !== "assistant") continue;
    current.push(block.item.markdown);
    if (block.endOfReply) texts.set(block.key, current.join("\n\n"));
  }
  return texts;
}

export function Transcript({
  blocks,
  memberId,
  working,
  showSpark,
  failure,
  streaming,
  pending,
  editedCards,
  answering,
  answerErrors,
  blocked,
  onAnswer,
  onUnavailable,
}: TranscriptProps) {
  const texts = replyTexts(blocks);
  const lastReplyKey = [...texts.keys()].at(-1);
  const sent = sentTimes(blocks);

  const render = (block: Block, next: Block | undefined): ReactNode => {
    switch (block.kind) {
      case "user":
        return <UserMessage text={block.item.text} />;
      case "assistant":
        return (
          <AssistantMessage
            markdown={block.item.markdown}
            actions={block.endOfReply && !(working && block.key === lastReplyKey)}
            replyText={texts.get(block.key) ?? block.item.markdown}
            latest={block.key === lastReplyKey}
            onUnavailable={onUnavailable}
          />
        );
      case "activity":
        return (
          <ActivityLine
            items={block.items}
            reply={next?.kind === "assistant" ? next.item.markdown : null}
            working={working}
          />
        );
      case "outreach":
        if (pending !== null && block.item.card_id === pending.cardId) return pending.node;
        return (
          <OutreachRecord
            item={block.item}
            sentAt={block.item.request_id === null ? null : (sent.get(block.item.request_id) ?? null)}
            edited={editedCards.has(block.item.card_id)}
          />
        );
      case "earlier":
        return <EarlierVersion item={block.item} />;
      case "request":
        return <RequestStatusCard requestId={block.item.request_id} memberId={memberId} />;
      case "question":
        return (
          <InlineQuestionCard
            item={block.item}
            busy={answering === block.item.escalation_id}
            blocked={blocked}
            error={answerErrors[block.item.escalation_id] ?? null}
            onAnswer={(text) => {
              onAnswer(block.item.escalation_id, text);
            }}
          />
        );
    }
  };

  const last = blocks.at(-1);
  return (
    <div className="flex flex-col">
      {blocks.map((block, index) => (
        <div key={block.key} className={cx(spacing(block, blocks[index - 1]), CARD_KINDS.has(block.kind) && "-mx-2")}>
          {render(block, blocks[index + 1])}
        </div>
      ))}
      {streaming !== null && streaming.text !== "" && (
        <div className={cx(last !== undefined && (last.kind === "user" ? "mt-12" : "mt-4"))}>
          <Markdown>{streaming.text}</Markdown>
        </div>
      )}
      {showSpark && (
        <div className={cx(last !== undefined && (last.kind === "user" && streaming === null ? "mt-12" : "mt-5"))}>
          <StreamingSpark />
        </div>
      )}
      {failure !== null && !showSpark && (
        <p role="alert" className={cx(last?.kind === "user" ? "mt-12" : "mt-5", "text-[14px] leading-6 text-danger")}>
          {failure}
        </p>
      )}
    </div>
  );
}
