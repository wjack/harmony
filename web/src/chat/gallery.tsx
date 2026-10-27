/**
 * The chat's /__gallery sections: the new-chat screen, Alex's conversation at each stage
 * of the story (over stateful fixtures, so its cards really work), the live demo's
 * variants (sending without review, a spent budget, long text), and each card on its
 * own in every state.
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
  requestAt,
  TURN,
  type ChatOptions,
  type ChatStage,
} from "../fixtures/chat";
import { budgets, members } from "../fixtures/shell";
import { useFixtureEmit } from "../gallery/fixtureContext";
import type { GallerySection } from "../gallery/types";
import { useUnavailable } from "../shell/unavailable";
import { InlineQuestionCard } from "./cards/InlineQuestionCard";
import { OutreachApproval } from "./cards/OutreachApproval";
import { OutreachCard } from "./cards/OutreachCard";
import { RequestStatusPanel } from "./cards/RequestStatusCard";
import { ChatScreen } from "./ChatScreen";
import { Composer } from "./Composer";
import { toBlocks, type ActivityItem } from "./items";
import { ActivityLine, AssistantMessage, StreamingSpark, UserMessage } from "./Messages";
import { NewChat } from "./NewChat";
import { initialDraft, type OutreachDraft } from "./outreachDraft";
import type { ChatItem, ChatItemOf, ChatItemType, SendMode } from "./types";

const noop = () => undefined;
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
      <ActivityLine items={activityOf("drafted")} />
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

function OutreachApprovalSection({ options = {} }: { options?: ChatOptions }) {
  const card = itemOf(itemsAt("drafted", {}, options), "outreach_card");
  const [log, setLog] = useState("Choose an option: nothing is sent from here.");
  const [feedback, setFeedback] = useState("");
  const [draft, setDraft] = useState<OutreachDraft>(() => initialDraft(card));
  return (
    <div className="mx-auto w-full max-w-[768px]">
      <OutreachApproval
        item={card}
        busy={false}
        error={null}
        draft={draft}
        onDraftChange={setDraft}
        onApprove={(body) => {
          setLog(`approve ${JSON.stringify(body)}`);
        }}
        feedback={feedback}
        onFeedbackChange={setFeedback}
        onReject={(feedback) => {
          setLog(`reject: ${feedback}`);
        }}
        onDismiss={() => {
          setLog("skipped");
        }}
      />
      <p className="mt-3 font-mono text-[12px] text-muted">{log}</p>
    </div>
  );
}

function InlineQuestionSection() {
  const open = itemOf(itemsAt("question"), "inline_question");
  const [, goAhead] = itemsOf(itemsAt("go_ahead"), "inline_question");
  const resolved = itemOf(itemsAt("go_ahead"), "inline_question");
  const [busy, setBusy] = useState(false);
  if (goAhead === undefined) throw new Error("no go-ahead question in the fixture");
  return (
    <Column>
      <InlineQuestionCard
        item={open}
        busy={busy}
        blocked={null}
        error={null}
        onAnswer={() => {
          setBusy(true);
        }}
      />
      <InlineQuestionCard
        item={goAhead}
        busy={false}
        blocked={null}
        error="Already answered by Alex Chen."
        onAnswer={noop}
      />
      <InlineQuestionCard item={resolved} busy={false} blocked={null} error={null} onAnswer={noop} />
      <InlineQuestionCard item={open} busy={false} blocked="Live models are paused" error={null} onAnswer={noop} />
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
    title: "Chat: outreach approval",
    description: "The request waits for Alex's OK as the ask-user card. Approving advances the fixture.",
    fixtures: chatRoutes("drafted"),
    ...PAGE,
    height: 860,
    render: () => <Chat />,
  },
  {
    title: "Chat: long brief and asks",
    description: "The brief and each ask clamp behind View more in the choose and subset views; the edit view's boxes grow.",
    fixtures: chatRoutes("drafted", { long: true }),
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
    description: "Running (shimmer) and finished; click to expand the steps.",
    render: () => (
      <Column>
        <ActivityLine items={activityOf("working")} />
        <ActivityLine items={activityOf("drafted")} />
      </Column>
    ),
  },
  {
    title: "Outreach card",
    description: "Waiting, sent, and not sent with feedback.",
    render: () => (
      <Column>
        <OutreachCard item={itemOf(itemsAt("drafted"), "outreach_card")} />
        <OutreachCard item={itemOf(itemsAt("sent"), "outreach_card")} />
        <OutreachCard
          item={itemOf(itemsAt("drafted", { feedback: "Leave Ridgeline out; we'll make the holder in-house." }), "outreach_card")}
        />
      </Column>
    ),
  },
  {
    title: "Outreach card: sent without review",
    description: "Sending, sent, and not sent with the reason; none waits on the member.",
    render: () => (
      <Column>
        <OutreachCard item={itemOf(itemsAt("drafted", {}, { auto: true }), "outreach_card")} />
        <OutreachCard item={itemOf(itemsAt("sent", {}, { auto: true }), "outreach_card")} />
        <OutreachCard
          item={itemOf(
            itemsAt("drafted", {}, { auto: true, autoFailure: "Calder Computational wasn't screened as a fit." }),
            "outreach_card",
          )}
        />
      </Column>
    ),
  },
  { title: "Outreach approval (ask-user card)", render: () => <OutreachApprovalSection /> },
  {
    title: "Outreach approval: long brief and asks",
    render: () => <OutreachApprovalSection options={{ long: true }} />,
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
