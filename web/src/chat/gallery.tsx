/**
 * The chat's /__gallery sections: the new-chat screen, Alex's conversation at each stage
 * of the story (over stateful fixtures, so its cards really work), and each card on
 * its own in every state.
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
  type ChatStage,
} from "../fixtures/chat";
import { members } from "../fixtures/shell";
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
import type { ChatItem, ChatItemOf, ChatItemType } from "./types";

const noop = () => undefined;
const ALEX = members.alex.id;

function itemsOf<T extends ChatItemType>(items: readonly ChatItem[], type: T): ChatItemOf<T>[] {
  return items.filter((item): item is ChatItemOf<T> => item.type === type);
}

function itemsAt(stage: ChatStage, patch: Partial<ReturnType<typeof initialState>> = {}): ChatItem[] {
  return conversationAt({ ...initialState(stage), ...patch }).items;
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

function NewChatSection() {
  const unavailable = useUnavailable();
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
  return (
    <div className="mx-auto flex w-full max-w-[768px] flex-col gap-8">
      <Composer
        variant="home"
        value={home}
        onChange={setHome}
        onSubmit={noop}
        placeholder="How can I help you today?"
        onUnavailable={unavailable}
      />
      <Composer value={reply} onChange={setReply} onSubmit={noop} placeholder="Reply…" onUnavailable={unavailable} />
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

function OutreachApprovalSection() {
  const card = itemOf(itemsAt("drafted"), "outreach_card");
  const [log, setLog] = useState("Choose an option: nothing is sent from here.");
  const [feedback, setFeedback] = useState("");
  return (
    <div className="mx-auto w-full max-w-[768px]">
      <OutreachApproval
        item={card}
        busy={false}
        error={null}
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
        error={null}
        onAnswer={() => {
          setBusy(true);
        }}
      />
      <InlineQuestionCard item={goAhead} busy={false} error="Already answered by Alex Chen." onAnswer={noop} />
      <InlineQuestionCard item={resolved} busy={false} error={null} onAnswer={noop} />
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
    title: "Chat: request sent",
    fixtures: chatRoutes("sent"),
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
  { title: "Composer", render: () => <ComposerSection /> },
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
  { title: "Outreach approval (ask-user card)", render: () => <OutreachApprovalSection /> },
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
    title: "Inline question",
    description: "A counterparty's question, a go-ahead on proposed terms (with an error), and an answered one.",
    render: () => <InlineQuestionSection />,
  },
] satisfies GallerySection[];
