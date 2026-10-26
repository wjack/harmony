import { ArrowDown, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { ApiError } from "../api/client";
import { useSession } from "../app/queries";
import { IconButton } from "../shell/ui/Button";
import { useUnavailable } from "../shell/unavailable";
import { cx } from "../shell/ui/cx";
import {
  isWorking,
  useAnyRequestActive,
  useApproveCard,
  useConversation,
  usePostMessage,
  useRejectCard,
  useResolveQuestion,
} from "./api";
import { OutreachApproval } from "./cards/OutreachApproval";
import { ChatHeader } from "./ChatHeader";
import { Composer } from "./Composer";
import { optimisticBlock, pendingCard, settledMessages, toBlocks } from "./items";
import { useChatStream, useConversationLive } from "./live";
import { removeOptimistic, useOptimistic } from "./optimistic";
import { outreachRecipients } from "./recipients";
import { plural } from "./format";
import { describeFailure, problemOf } from "./problem";
import { Transcript } from "./Transcript";
import type { ApproveCard, ConversationDetail, OutreachCardItem } from "./types";
import { useStickToBottom } from "./useStickToBottom";

/** The "Want to be notified…" strip, attached to the top of the composer. */
function ComposerBanner({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  return (
    <div className="mx-2.5 -mb-3 flex items-center gap-3 rounded-t-[14px] bg-bubble pt-2 pr-2 pb-5 pl-4 text-[14px] text-secondary animate-fade-in">
      <span className="min-w-0 flex-1">{children}</span>
      <IconButton label="Dismiss" size="sm" onClick={onClose}>
        <X size={15} strokeWidth={1.6} />
      </IconButton>
    </div>
  );
}

function cardTitle(card: OutreachCardItem): string {
  return `Send this request to ${plural(outreachRecipients(card).proposed.size, "organization")}?`;
}

function ChatView({ detail, conversationId }: { detail: ConversationDetail; conversationId: string }) {
  const session = useSession();
  const memberId = session.data?.member.id ?? null;
  const unavailable = useUnavailable();
  const optimistic = useOptimistic(conversationId);

  const post = usePostMessage(conversationId);
  const approve = useApproveCard(conversationId);
  const reject = useRejectCard(conversationId);
  const resolve = useResolveQuestion(conversationId);

  const [draft, setDraft] = useState("");
  const [dismissedCard, setDismissedCard] = useState<string | null>(null);
  const [bannerClosed, setBannerClosed] = useState(false);
  const [answerErrors, setAnswerErrors] = useState<Record<string, string>>({});
  /** The card's "Something else" text by card id, so putting the card away keeps it. */
  const [cardFeedback, setCardFeedback] = useState<Record<string, string>>({});

  useConversationLive(conversationId, detail.items);

  // Drop sent messages once the conversation shows them.
  const settled = settledMessages(detail.items, optimistic);
  const settledIds = [...settled].join(",");
  useEffect(() => {
    if (settledIds !== "") removeOptimistic(conversationId, settledIds.split(","));
  }, [conversationId, settledIds]);

  const unsent = optimistic.filter((message) => !settled.has(message.messageId));
  const blocks = [...toBlocks(detail.items), ...unsent.map(optimisticBlock)];

  const card = pendingCard(detail);
  const cardId = card?.card_id ?? null;
  // A card that turns up while the chat is open. Mid-draft, it waits behind the
  // composer's strip instead of taking the draft's place; otherwise it opens, but
  // ignores picks for a moment (`AskUserCard`'s `arriving`).
  const [shownCard, setShownCard] = useState(cardId);
  const [arrivedCard, setArrivedCard] = useState<string | null>(null);
  if (cardId !== shownCard) {
    setShownCard(cardId);
    if (cardId !== null) {
      setArrivedCard(cardId);
      if (draft.trim() !== "") setDismissedCard(cardId);
    }
  }
  const cardOpen = card !== null && dismissedCard !== card.card_id;
  const working = isWorking(detail) || unsent.length > 0;
  const streaming = useChatStream(conversationId, detail.items, working);
  const showSpark = working && card === null;

  const requestIds = detail.items.flatMap((item) => (item.type === "request_status" ? [item.request_id] : []));
  // Once every request has closed, nothing more will arrive.
  const awaitingNetwork = useAnyRequestActive(requestIds);
  const showBanner = awaitingNetwork && !bannerClosed && !cardOpen;

  const { scrollRef, contentRef, atBottom, onScroll, toBottom } = useStickToBottom([
    blocks.length,
    streaming?.text,
    detail.version,
    cardOpen,
  ]);

  // Mutation state belongs to the card it was for, not to whichever card is up now.
  const forCard = (variables: { cardId: string } | undefined) => cardId !== null && variables?.cardId === cardId;
  const cardError = describeCardError(
    (forCard(approve.variables) ? approve.error : null) ?? (forCard(reject.variables) ? reject.error : null),
  );
  const cardBusy = (approve.isPending && forCard(approve.variables)) || (reject.isPending && forCard(reject.variables));
  const feedbackProps = (id: string) => ({
    feedback: cardFeedback[id] ?? "",
    onFeedbackChange: (text: string) => {
      setCardFeedback((current) => ({ ...current, [id]: text }));
    },
    arriving: arrivedCard === id,
  });
  const approveCard = (id: string, body: ApproveCard) => {
    reject.reset();
    approve.mutate({ cardId: id, body });
  };
  const rejectCard = (id: string, feedback: string) => {
    approve.reset();
    reject.mutate({ cardId: id, feedback });
  };

  const send = (text: string) => {
    setDraft("");
    post.mutate(text, {
      onError: () => {
        setDraft((current) => (current === "" ? text : current));
      },
    });
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas">
      <ChatHeader title={detail.title} onUnavailable={unavailable} />
      <div ref={scrollRef} onScroll={onScroll} className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto">
        <div ref={contentRef} className="mx-auto w-full max-w-[768px] px-5 pt-6 pb-12 sm:px-6">
          <Transcript
            blocks={blocks}
            memberId={memberId}
            working={working}
            showSpark={showSpark}
            failed={detail.turn_failed && !working}
            streaming={streaming}
            answering={resolve.isPending ? resolve.variables.escalationId : null}
            answerErrors={answerErrors}
            onAnswer={(escalationId, text) => {
              setAnswerErrors((current) => without(current, escalationId));
              resolve.mutate(
                { escalationId, body: { action: "approve", text } },
                {
                  onError: (error) => {
                    setAnswerErrors((current) => ({ ...current, [escalationId]: describeFailure(error) }));
                  },
                },
              );
            }}
            onUnavailable={unavailable}
          />
        </div>
      </div>
      <div className="relative mx-auto w-full max-w-[768px] shrink-0 px-3 pb-3 sm:px-0">
        <div aria-hidden className="pointer-events-none absolute inset-x-0 -top-6 h-6 bg-linear-to-t from-canvas" />
        {!atBottom && (
          <button
            type="button"
            aria-label="Scroll to the latest message"
            onClick={() => {
              toBottom("smooth");
            }}
            className="absolute -top-12 left-1/2 z-10 inline-flex size-8 -translate-x-1/2 items-center justify-center rounded-full border border-border bg-surface text-secondary shadow-card transition-colors hover:bg-hover animate-fade-in"
          >
            <ArrowDown size={16} strokeWidth={1.8} />
          </button>
        )}
        {cardOpen ? (
          <OutreachApproval
            key={card.card_id}
            item={card}
            busy={cardBusy}
            error={cardError}
            {...feedbackProps(card.card_id)}
            onApprove={(body) => {
              approveCard(card.card_id, body);
            }}
            onReject={(feedback) => {
              rejectCard(card.card_id, feedback);
            }}
            onDismiss={() => {
              setDismissedCard(card.card_id);
            }}
          />
        ) : (
          <>
            {showBanner && (
              <ComposerBanner
                onClose={() => {
                  setBannerClosed(true);
                }}
              >
                Responses from the network will appear here as they arrive.
              </ComposerBanner>
            )}
            {card !== null && (
              <button
                type="button"
                onClick={() => {
                  setDismissedCard(null);
                  setArrivedCard(null);
                }}
                className="mx-2.5 -mb-3 flex w-[calc(100%-20px)] items-center gap-2 rounded-t-[14px] bg-bubble pt-2 pr-3 pb-5 pl-4 text-left text-[14px] text-secondary transition-colors hover:text-ink"
              >
                <span className="min-w-0 flex-1 truncate">{cardTitle(card)}</span>
                <span className="shrink-0 rounded-control bg-primary px-2.5 py-1 text-[13px] font-medium text-on-primary">
                  Review
                </span>
              </button>
            )}
            <Composer
              className={cx("relative", (showBanner || card !== null) && "z-10")}
              value={draft}
              onChange={setDraft}
              onSubmit={send}
              sending={post.isPending}
              autoFocus
              placeholder={card === null ? "Reply…" : "Tell Claude what to change instead…"}
              onUnavailable={unavailable}
            />
            {post.isError && (
              <p role="alert" className="mt-2 text-center text-[12.5px] text-danger">
                {describeFailure(post.error)}
              </p>
            )}
            <p className="mt-2 text-center text-[12px] text-muted">
              Claude is AI and can make mistakes. Please double-check responses.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

function without(record: Readonly<Record<string, string>>, key: string): Record<string, string> {
  return Object.fromEntries(Object.entries(record).filter(([entry]) => entry !== key));
}

function describeCardError(error: Error | null): string | null {
  if (error === null) return null;
  // Someone (or another tab) already answered: the refetch shows how.
  if (problemOf(error)?.code === "card_not_pending") return null;
  return describeFailure(error);
}

function ChatMessage({ title, body }: { title: string; body: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
      <h1 className="font-serif text-[22px] text-ink">{title}</h1>
      <p className="max-w-sm text-[14px] text-muted">{body}</p>
    </div>
  );
}

/** `/chat/:id`: one conversation with Claude, its cards, and the composer. */
export function ChatScreen({ conversationId }: { conversationId: string }) {
  const conversation = useConversation(conversationId);
  if (conversation.data !== undefined) {
    return <ChatView detail={conversation.data} conversationId={conversationId} />;
  }
  if (conversation.isError) {
    return conversation.error instanceof ApiError && conversation.error.status === 404 ? (
      <ChatMessage title="This chat isn't here" body="It may belong to another account, or the demo was reset." />
    ) : (
      <ChatMessage title="Couldn't open this chat" body={describeFailure(conversation.error)} />
    );
  }
  return <div className="h-full bg-canvas" aria-busy="true" />;
}
