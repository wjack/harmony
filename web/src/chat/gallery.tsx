/**
 * The chat's /__gallery sections: the new-chat screen, Alex's conversation at each stage
 * of the story (over stateful fixtures, so its cards really work), the live demo's
 * variants (sending without review, a spent budget, a card declined or redrafted, a
 * message Claude declined), and each card on its own in every state.
 */
import { useEffect, useState, type ReactNode } from "react";

import {
  chatDeltaEnvelope,
  chatRoutes,
  conversationAt,
  CONVERSATION_ID,
  FIRST_REPLY,
  initialState,
  OPENING_MESSAGE,
  REDRAFT_MESSAGE,
  requestAt,
  TURN,
  type ChatOptions,
  type ChatStage,
  type Rejection,
} from "../fixtures/chat";
import { budgets, members } from "../fixtures/shell";
import { useFixtureEmit } from "../gallery/fixtureContext";
import type { GallerySection } from "../gallery/types";
import { useUnavailable } from "../shell/unavailable";
import { InlineQuestionCard } from "./cards/InlineQuestionCard";
import { EarlierVersion, OutreachCard, OutreachRecord } from "./cards/OutreachCard";
import { RequestStatusPanel } from "./cards/RequestStatusCard";
import { ChatScreen } from "./ChatScreen";
import { Composer } from "./Composer";
import { DeclinedTurn } from "./DeclinedTurn";
import { toBlocks, type ActivityItem } from "./items";
import { ActivityLine, AssistantMessage, StreamingSpark, UserMessage } from "./Messages";
import { NewChat } from "./NewChat";
import { budgetLine, declinedLine } from "./failureCopy";
import { initialDraft, partVersions, type OutreachDraft } from "./outreachDraft";
import type { ChatItem, ChatItemOf, ChatItemType, InlineQuestionItem, OutreachCardItem, SendMode } from "./types";

const noop = () => undefined;

/** A follow-up of Alex's that the gallery has Claude decline. */
const REFUSED_MESSAGE = "Also ask Northlake to test two of the coupons to failure, so we can see where they give.";
const ALEX = members.alex.id;

function itemsOf<T extends ChatItemType>(items: readonly ChatItem[], type: T): ChatItemOf<T>[] {
  return items.filter((item): item is ChatItemOf<T> => item.type === type);
}

function itemsAt(
  stage: ChatStage,
  patch: Partial<ReturnType<typeof initialState>> = {},
  options: ChatOptions = {},
): ChatItem[] {
  return conversationAt({ ...initialState(stage, options), ...patch }).items;
}

/** A send mode the section keeps itself, over the organization's `orgDefault`. */
function useLocalSendMode(orgDefault: boolean, initial: boolean | null = null) {
  const [override, setOverride] = useState(initial);
  const sendMode: SendMode = { override, org_default: orgDefault, effective: override ?? orgDefault };
  return { sendMode, onSendModeChange: setOverride };
}

function itemOf<T extends ChatItemType>(items: readonly ChatItem[], type: T): ChatItemOf<T> {
  const found = items.find((item): item is ChatItemOf<T> => item.type === type);
  if (found === undefined) throw new Error(`no ${type} item in the fixture`);
  return found;
}

function activityOf(stage: ChatStage): ActivityItem[] {
  const block = toBlocks(itemsAt(stage)).find((candidate) => candidate.kind === "activity");
  return block?.kind === "activity" ? block.items : [];
}

/** The transcript column's width, so a card on its own reads at its real size. */
function Column({ children }: { children: ReactNode }) {
  return <div className="mx-auto flex w-full max-w-[720px] flex-col gap-6">{children}</div>;
}

function NewChatSection({ orgDefault = false }: { orgDefault?: boolean }) {
  const unavailable = useUnavailable();
  const sendMode = useLocalSendMode(orgDefault);
  const [sending, setSending] = useState(false);
  return (
    <NewChat
      memberName={members.alex.name}
      suggestion={OPENING_MESSAGE}
      sending={sending}
      error={null}
      hour={10}
      onSend={() => {
        setSending(true);
      }}
      onUnavailable={unavailable}
      {...sendMode}
    />
  );
}

function Chat() {
  return <ChatScreen conversationId={CONVERSATION_ID} />;
}

/** Streams Claude's first reply into the working chat, word by word, then retries it as attempt 2, 3… */
function StreamingChat() {
  const emit = useFixtureEmit();

  useEffect(() => {
    const words = FIRST_REPLY.split(/(?<=\s)/);
    let attempt = 1;
    let index = 0;
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      if (index < words.length) {
        emit(chatDeltaEnvelope(TURN.search, attempt, words.slice(index, index + 3).join("")));
        index += 3;
        timer = setTimeout(tick, 90);
        return;
      }
      // A retried attempt replaces the text, as the stream's attempt counter does live.
      attempt += 1;
      index = 0;
      timer = setTimeout(tick, 2_500);
    };
    timer = setTimeout(tick, 600);
    return () => {
      clearTimeout(timer);
    };
  }, [emit]);

  return <Chat />;
}

function ComposerSection() {
  const unavailable = useUnavailable();
  const [home, setHome] = useState("");
  const [reply, setReply] = useState("Can Northlake do it by Nov 4 instead?");
  const [auto, setAuto] = useState("");
  const reviewed = useLocalSendMode(false);
  const replyMode = useLocalSendMode(false);
  const unreviewed = useLocalSendMode(true);
  return (
    <div className="mx-auto flex w-full max-w-[768px] flex-col gap-8">
      <Composer
        variant="home"
        value={home}
        onChange={setHome}
        onSubmit={noop}
        placeholder="How can I help you today?"
        onUnavailable={unavailable}
        {...reviewed}
      />
      <Composer
        value={reply}
        onChange={setReply}
        onSubmit={noop}
        placeholder="Reply…"
        onUnavailable={unavailable}
        {...replyMode}
      />
      <Composer
        value={auto}
        onChange={setAuto}
        onSubmit={noop}
        placeholder="Reply…"
        onUnavailable={unavailable}
        {...unreviewed}
      />
    </div>
  );
}

/** The composer while today's budget is spent: the strip says so, and nothing sends. */
function BlockedComposerSection() {
  const unavailable = useUnavailable();
  const [reply, setReply] = useState("Can Northlake do it by Nov 4 instead?");
  const sendMode = useLocalSendMode(false);
  return (
    <div className="mx-auto w-full max-w-[768px] pt-6">
      <Composer
        value={reply}
        onChange={setReply}
        onSubmit={noop}
        placeholder="Reply…"
        onUnavailable={unavailable}
        {...sendMode}
      />
    </div>
  );
}

function MessagesSection() {
  const unavailable = useUnavailable();
  const items = itemsAt("drafted");
  const first = itemOf(items, "user_text");
  const answer = itemOf(items, "assistant_text");
  return (
    <Column>
      <UserMessage text={first.text} />
      <ActivityLine items={activityOf("drafted")} working={false} />
      <AssistantMessage
        markdown={answer.markdown}
        actions
        latest
        replyText={answer.markdown}
        onUnavailable={unavailable}
      />
      <StreamingSpark />
    </Column>
  );
}

/** Alex turns the first draft down with a message, and Claude redrafts it. */
const REDRAFTED: Rejection = { with: "message", text: REDRAFT_MESSAGE };
/** Alex declines the first draft with a reason. */
const DECLINED: Rejection = { with: "decline", text: "Not until the PI confirms the budget." };

/** Every outreach card in a fixture conversation, oldest first. */
function cardsAt(stage: ChatStage, options: ChatOptions = {}): OutreachCardItem[] {
  return itemsOf(itemsAt(stage, {}, options), "outreach_card");
}

interface CardPreset {
  /** Changes to Claude's draft, as if the member had made them. */
  edit?: (draft: OutreachDraft, card: OutreachCardItem) => OutreachDraft;
  folded?: boolean;
  declineOpen?: boolean;
  blocked?: boolean;
  options?: ChatOptions;
}

/** The waiting card on its own, with its own state: what Approve or Decline would send shows under it. */
function OutreachCardSection({ edit, folded: initiallyFolded = false, declineOpen: open = false, blocked = false, options = {} }: CardPreset) {
  const items = itemsAt("drafted", {}, options);
  const card = itemsOf(items, "outreach_card").at(-1);
  if (card === undefined) throw new Error("no outreach card in the fixture");
  const sendMode = useLocalSendMode(false);
  const [draft, setDraft] = useState<OutreachDraft>(() => (edit ?? ((d) => d))(initialDraft(card), card));
  const [folded, setFolded] = useState(initiallyFolded);
  const [declineOpen, setDeclineOpen] = useState(open);
  const [log, setLog] = useState("Nothing is sent from here.");
  return (
    <div>
      <OutreachCard
        item={card}
        versions={partVersions(items, card)}
        draft={draft}
        onDraftChange={setDraft}
        {...sendMode}
        folded={folded}
        onFoldChange={setFolded}
        busy={false}
        declineOpen={declineOpen}
        onDeclineOpenChange={setDeclineOpen}
        declinePending={false}
        error={null}
        blocked={blocked ? budgetLine("daily_exhausted") : null}
        arriving={false}
        onApprove={(body) => {
          setLog(`approve ${JSON.stringify(body)}`);
        }}
        onDecline={(reason) => {
          setLog(`decline ${JSON.stringify({ feedback: reason })}`);
        }}
      />
      <p className="mt-2 font-mono text-[12px] break-all text-muted">{log}</p>
    </div>
  );
}

/** Alex's edits for the "edited" state: Ridgeline left out, the brief and Northlake's ask reworded. */
function editedDraft(draft: OutreachDraft, card: OutreachCardItem): OutreachDraft {
  const [northlake, ridgeline] = card.recipients;
  const chosen = new Set(draft.chosen);
  if (ridgeline !== undefined) chosen.delete(ridgeline.org.id);
  return {
    ...draft,
    chosen,
    brief: draft.brief.replace("with results by Nov 6", "with results by Nov 5"),
    asks:
      northlake === undefined
        ? draft.asks
        : { ...draft.asks, [northlake.org.id]: (northlake.ask ?? "").replace("by Nov 6", "by Nov 5") },
  };
}

/** The question cards in the chat at `stage`, as the transcript draws them. */
function questionsAt(stage: ChatStage): InlineQuestionItem[] {
  return toBlocks(itemsAt(stage)).flatMap((block) => (block.kind === "question" ? [block.item] : []));
}

function InlineQuestionSection() {
  const [open] = questionsAt("question");
  const [resolved, goAhead] = questionsAt("go_ahead");
  const [busy, setBusy] = useState(false);
  if (open === undefined || resolved === undefined || goAhead === undefined) {
    throw new Error("no questions in the fixture");
  }
  const us = "Henderson Frame Lab";
  return (
    <Column>
      <InlineQuestionCard
        item={open}
        us={us}
        busy={busy}
        blocked={null}
        error={null}
        onAnswer={() => {
          setBusy(true);
        }}
      />
      <InlineQuestionCard
        item={goAhead}
        us={us}
        busy={false}
        blocked={null}
        error="Already answered by Alex Chen."
        onAnswer={noop}
      />
      <InlineQuestionCard item={resolved} us={us} busy={false} blocked={null} error={null} onAnswer={noop} />
      <InlineQuestionCard item={open} us={us} busy={false} blocked="Live models are paused" error={null} onAnswer={noop} />
    </Column>
  );
}

const PAGE = { layout: "page", height: 760 } as const;

export default [
  {
    title: "New chat",
    description: "Greeting, composer, starter chips and the walkthrough's suggested message.",
    layout: "page",
    height: 640,
    render: () => <NewChatSection />,
  },
  {
    title: "New chat: organization sends without review",
    description: "Henderson's default is to send without review: the amber chip shows; its menu changes this chat only.",
    layout: "page",
    height: 640,
    render: () => <NewChatSection orgDefault />,
  },
  {
    title: "Chat: Claude working, streaming",
    description: "A running search on the activity line; stream.chat_delta frames stream the reply in.",
    fixtures: chatRoutes("working"),
    ...PAGE,
    render: () => <StreamingChat />,
  },
  {
    title: "Chat: request waiting for you",
    description:
      "Claude's request is a card at the end of the chat; the box at the bottom asks what to do differently. Approving advances the fixture.",
    fixtures: chatRoutes("drafted"),
    ...PAGE,
    height: 900,
    render: () => <Chat />,
  },
  {
    title: "Chat: request redrafted",
    description: "Alex's message turned the first draft down: it folds to an Earlier version row, and Claude's redraft marks what it rewrote.",
    fixtures: chatRoutes("drafted", { rejected: REDRAFTED }),
    ...PAGE,
    height: 900,
    render: () => <Chat />,
  },
  {
    title: "Chat: request declined",
    description: "Declined with a reason only Claude in this chat sees: the card folds to a Declined record and Claude says one line.",
    fixtures: chatRoutes("drafted", { rejected: DECLINED }),
    ...PAGE,
    render: () => <Chat />,
  },
  {
    title: "Chat: request waiting, budget used up",
    description: "The strip sits on the card, not the box: Approve, Decline and the box hold; ticks and wording still work.",
    fixtures: { ...chatRoutes("drafted"), "GET /api/budget": budgets.dailyExhausted },
    ...PAGE,
    height: 900,
    render: () => <Chat />,
  },
  {
    title: "Chat: sending without review",
    description: "This chat sends without review: Claude's request goes out with no card to approve.",
    fixtures: chatRoutes("drafted", { auto: true, override: true }),
    ...PAGE,
    render: () => <Chat />,
  },
  {
    title: "Chat: sent without review",
    fixtures: chatRoutes("sent", { auto: true, override: true }),
    ...PAGE,
    render: () => <Chat />,
  },
  {
    title: "Chat: request sent",
    fixtures: chatRoutes("sent"),
    ...PAGE,
    render: () => <Chat />,
  },
  {
    title: "Chat: today's budget is used up",
    description: "Claude's turn stopped on the daily cap and Ridgeline's thread with it; the composer waits for the reset.",
    fixtures: {
      ...chatRoutes("sent", { turnFailure: "daily_budget", stopReason: "daily_budget" }),
      "GET /api/budget": budgets.dailyExhausted,
    },
    ...PAGE,
    height: 820,
    render: () => <Chat />,
  },
  {
    title: "Chat: model error",
    description: "The model service failed after its retries; the next message tries again.",
    fixtures: chatRoutes("sent", { turnFailure: "model_error" }),
    ...PAGE,
    render: () => <Chat />,
  },
  {
    title: "Chat: Claude declined a message",
    description:
      "Claude declined Alex's follow-up (biosecurity). The note sits under the message: Retry sends it again as it was, and Edit opens it to reword first.",
    fixtures: chatRoutes("sent", { refused: { text: REFUSED_MESSAGE, category: "bio" } }),
    ...PAGE,
    render: () => <Chat />,
  },
  {
    title: "Declined message",
    description:
      "The note on its own: with a category; two messages sent together, after a Retry that didn't go through; and while the budget is used up, when Retry waits but Edit still opens.",
    render: () => (
      // Wider apart than a transcript's turns, so each example reads on its own.
      <div className="mx-auto flex w-full max-w-[720px] flex-col gap-16">
        <DeclinedTurn
          texts={[REFUSED_MESSAGE]}
          note={declinedLine("respond to this", "cyber")}
          busy={false}
          error={null}
          blocked={null}
          onRetry={noop}
        />
        <DeclinedTurn
          texts={[OPENING_MESSAGE, REFUSED_MESSAGE]}
          note={declinedLine("respond to this", null)}
          busy={false}
          error="That didn't go through. Try again."
          blocked={null}
          onRetry={noop}
        />
        <DeclinedTurn
          texts={[REFUSED_MESSAGE]}
          note={declinedLine("respond to this", "bio")}
          busy={false}
          error={null}
          blocked={budgetLine("daily_exhausted")}
          onRetry={noop}
        />
      </div>
    ),
  },
  {
    title: "Chat: Ridgeline asks Alex",
    description: "Ridgeline's question reaches Alex inline; answering it resolves the consult.",
    fixtures: chatRoutes("question"),
    ...PAGE,
    render: () => <Chat />,
  },
  {
    title: "Chat: go-ahead questions",
    description: "Both representatives have proposed terms; answering both closes the threads.",
    fixtures: chatRoutes("go_ahead"),
    ...PAGE,
    height: 900,
    render: () => <Chat />,
  },
  {
    title: "Chat: closed",
    description: "Both threads closed; Claude sums up.",
    fixtures: chatRoutes("closed"),
    ...PAGE,
    height: 900,
    render: () => <Chat />,
  },
  {
    title: "Chat: not found",
    layout: "page",
    height: 320,
    render: () => <ChatScreen conversationId="0b1d7a3e-0000-4000-8000-00000000ffff" />,
  },
  {
    title: "Composer",
    description: "Home and reply, then a chat that sends without review: the chip opens the send-mode menu.",
    render: () => <ComposerSection />,
  },
  {
    title: "Composer: budget used up",
    fixtures: { "GET /api/budget": budgets.dailyExhausted },
    render: () => <BlockedComposerSection />,
  },
  { title: "Messages and activity", render: () => <MessagesSection /> },
  {
    title: "Activity line",
    description: "A search still running (shimmer), then the finished turn; click either to see each step's outcome and what it searched for.",
    render: () => (
      <Column>
        <ActivityLine items={activityOf("working")} working />
        <ActivityLine items={activityOf("drafted")} working={false} />
      </Column>
    ),
  },
  {
    title: "Outreach card: waiting",
    render: () => (
      <Column>
        <OutreachCardSection />
      </Column>
    ),
  },
  {
    title: "Outreach card: edited",
    description: "Ridgeline left out, the brief and Northlake's ask reworded: each changed part says so, with Undo my edits.",
    render: () => (
      <Column>
        <OutreachCardSection edit={editedDraft} />
      </Column>
    ),
  },
  {
    title: "Outreach card: rewritten by Claude",
    description: "Claude's redraft after a message: the parts it changed page back to its earlier wording.",
    render: () => (
      <Column>
        <OutreachCardSection options={{ rejected: REDRAFTED }} />
      </Column>
    ),
  },
  {
    title: "Outreach card: folded, declining, budget used up",
    render: () => (
      <Column>
        <OutreachCardSection folded />
        <OutreachCardSection declineOpen />
        <OutreachCardSection blocked />
      </Column>
    ),
  },
  {
    title: "Outreach card: after the decision",
    description: "Sent with the member's edits, declined with a reason, and a draft Claude replaced; Show reads each in full.",
    render: () => {
      const [replaced] = cardsAt("drafted", { rejected: REDRAFTED });
      const [sent] = cardsAt("sent");
      const [declined] = cardsAt("drafted", { rejected: DECLINED });
      if (replaced === undefined || sent === undefined || declined === undefined) throw new Error("missing card");
      return (
        <Column>
          <OutreachRecord item={sent} sentAt={sent.at} edited />
          <OutreachRecord item={declined} sentAt={null} />
          <EarlierVersion item={replaced} />
        </Column>
      );
    },
  },
  {
    title: "Outreach card: sent without review",
    description: "Sending, sent, and not sent with the reason; none waits on the member.",
    render: () => {
      const [sending] = cardsAt("drafted", { auto: true });
      const [sent] = cardsAt("sent", { auto: true });
      const [failed] = cardsAt("drafted", { auto: true, autoFailure: "Calder Computational wasn't screened as a fit." });
      if (sending === undefined || sent === undefined || failed === undefined) throw new Error("missing card");
      return (
        <Column>
          <OutreachRecord item={sending} sentAt={null} />
          <OutreachRecord item={sent} sentAt={sent.at} />
          <OutreachRecord item={failed} sentAt={null} />
        </Column>
      );
    },
  },
  {
    title: "Request status",
    description: "Sent; waiting on people at both ends; both closed with Henderson's summaries.",
    render: () => (
      <Column>
        <RequestStatusPanel view={requestAt(initialState("sent"))} memberId={ALEX} />
        <RequestStatusPanel view={requestAt(initialState("question"))} memberId={ALEX} />
        <RequestStatusPanel view={requestAt(initialState("closed"))} memberId={ALEX} />
      </Column>
    ),
  },
  {
    title: "Request status: stopped",
    description: "Ridgeline's thread stopped on today's cap; the line says why, by the budget's state now.",
    fixtures: { "GET /api/budget": budgets.dailyExhausted },
    render: () => (
      <Column>
        <RequestStatusPanel view={requestAt(initialState("sent", { stopReason: "daily_budget" }))} memberId={ALEX} />
        <RequestStatusPanel view={requestAt(initialState("sent", { stopReason: "refused" }))} memberId={ALEX} />
      </Column>
    ),
  },
  {
    title: "Inline question",
    description: "A counterparty's question, a go-ahead on proposed terms (with an error), and an answered one.",
    render: () => <InlineQuestionSection />,
  },
] satisfies GallerySection[];
