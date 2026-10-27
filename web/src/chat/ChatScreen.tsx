import { ArrowDown, X } from "lucide-react";
import { useEffect, useEffectEvent, useState, type ReactNode } from "react";

import { ApiError } from "../api/client";
import { useSession } from "../app/queries";
import { WhoseNotFound } from "../demo/WhoseNotFound";
import { useSeenInChat } from "../requests/seen";
import { IconButton } from "../shell/ui/Button";
import { cx } from "../shell/ui/cx";
import { useUnavailable } from "../shell/unavailable";
import {
  isWorking,
  useAnyRequestActive,
  useApproveCard,
  useConversation,
  usePostMessage,
  useRejectCard,
  useResolveQuestion,
  useSetSendMode,
} from "./api";
import { OutreachCard } from "./cards/OutreachCard";
import { ChatHeader } from "./ChatHeader";
import { Composer } from "./Composer";
import { budgetLine, failureLine, useBudgetState } from "./failureCopy";
import { optimisticBlock, pendingCard, settledMessages, toBlocks } from "./items";
import { useChatStream, useConversationLive } from "./live";
import { removeOptimistic, useOptimistic } from "./optimistic";
import { carriesEdits, initialDraft, outreachTitle, partVersions, type OutreachDraft } from "./outreachDraft";
import { describeFailure, problemOf } from "./problem";
import { Transcript } from "./Transcript";
import type { ApproveCard, ConversationDetail } from "./types";
import { useInView } from "./useInView";
import { useStickToBottom } from "./useStickToBottom";

/** The room left above the card when Review scrolls to it. */
const REVIEW_MARGIN_PX = 16;

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

function ChatView({ detail, conversationId }: { detail: ConversationDetail; conversationId: string }) {
  const session = useSession();
  const memberId = session.data?.member.id ?? null;
  useSeenInChat(conversationId, session.data?.member.id);
  const unavailable = useUnavailable();
  const optimistic = useOptimistic(conversationId);

  const post = usePostMessage(conversationId);
  const approve = useApproveCard(conversationId);
  const reject = useRejectCard(conversationId);
  const resolve = useResolveQuestion(conversationId);
  const setSendMode = useSetSendMode(conversationId);
  const budgetState = useBudgetState();
  const blocked = budgetLine(budgetState);

  const [draft, setDraft] = useState("");
  const [bannerClosed, setBannerClosed] = useState(false);
  const [answerErrors, setAnswerErrors] = useState<Record<string, string>>({});
  /** The card's ticks and rewording by card id, so folding it or a refetch keeps them. */
  const [cardDrafts, setCardDrafts] = useState<Record<string, OutreachDraft>>({});
  const [foldedCards, setFoldedCards] = useState<ReadonlySet<string>>(new Set());
  // Approved cards read as sent, so the record can't tell an edit from Claude's draft;
  // the chat remembers which approvals carried the member's own wording.
  const [editedCards, setEditedCards] = useState<ReadonlySet<string>>(new Set());
  /** The card whose Decline step is open, if any. */
  const [decliningCard, setDecliningCard] = useState<string | null>(null);
  /** The waiting card's box, once drawn: scrolled to on arrival, watched for the Review strip. */
  const [cardElement, setCardElement] = useState<HTMLDivElement | null>(null);

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
  // A card that turns up while the chat is open holds Approve for a moment
  // (`ARRIVAL_PAUSE_MS`); one already there when the chat opened doesn't.
  const [shownCard, setShownCard] = useState(cardId);
  const [arrivedCard, setArrivedCard] = useState<string | null>(null);
  if (cardId !== shownCard) {
    setShownCard(cardId);
    if (cardId !== null) setArrivedCard(cardId);
  }
  const cardDraft = card === null ? null : (cardDrafts[card.card_id] ?? initialDraft(card));
  const working = isWorking(detail) || unsent.length > 0;
  const streaming = useChatStream(conversationId, detail.items, working);
  const showSpark = working && card === null;

  const requestIds = detail.items.flatMap((item) => (item.type === "request_status" ? [item.request_id] : []));
  // Once every request has closed, nothing more will arrive.
  const awaitingNetwork = useAnyRequestActive(requestIds);
  // The composer's own budget strip says more than this one while a cap is spent.
  const showBanner = awaitingNetwork && !bannerClosed && card === null && blocked === null;

  const { scrollRef, contentRef, atBottom, onScroll, toBottom, revealTop } = useStickToBottom([
    blocks.length,
    streaming?.text,
    detail.version,
  ]);

  // A card arriving, or the chat opening on one, shows the card from its top.
  const revealCard = useEffectEvent(revealTop);
  useEffect(() => {
    if (cardElement !== null) revealCard(cardElement);
  }, [cardElement]);
  const cardInView = useInView(cardElement, scrollRef);

  // Mutation state belongs to the card it was for, not to whichever card is up now.
  const forCard = (variables: { cardId: string } | undefined) => cardId !== null && variables?.cardId === cardId;
  const cardError = describeCardError(
    (forCard(approve.variables) ? approve.error : null) ?? (forCard(reject.variables) ? reject.error : null),
  );
  const declinePending = reject.isPending && forCard(reject.variables);
  const cardBusy = (approve.isPending && forCard(approve.variables)) || declinePending;

  const setFolded = (id: string, folded: boolean) => {
    setFoldedCards((current) => toggled(current, id, folded));
  };

  /** The Review strip: open the card and scroll its top into view. */
  const review = (id: string) => {
    setFolded(id, false);
    const viewport = scrollRef.current;
    if (viewport === null || cardElement === null) return;
    const top = cardElement.getBoundingClientRect().top - viewport.getBoundingClientRect().top + viewport.scrollTop;
    viewport.scrollTo({ top: Math.max(0, top - REVIEW_MARGIN_PX), behavior: "smooth" });
  };

  const send = (text: string) => {
    setDraft("");
    post.mutate(text, {
      onError: () => {
        setDraft((current) => (current === "" ? text : current));
      },
    });
  };

  const pending =
    card === null || cardDraft === null
      ? null
      : {
          cardId: card.card_id,
          node: (
            <OutreachCard
              key={card.card_id}
              ref={setCardElement}
              item={card}
              versions={partVersions(detail.items, card)}
              draft={cardDraft}
              onDraftChange={(update) => {
                setCardDrafts((current) => ({
                  ...current,
                  [card.card_id]: update(current[card.card_id] ?? initialDraft(card)),
                }));
              }}
              sendMode={detail.send_mode}
              onSendModeChange={(override) => {
                setSendMode.mutate(override);
              }}
              folded={foldedCards.has(card.card_id)}
              onFoldChange={(folded) => {
                setFolded(card.card_id, folded);
              }}
              busy={cardBusy}
              declineOpen={decliningCard === card.card_id}
              onDeclineOpenChange={(open) => {
                setDecliningCard(open ? card.card_id : null);
              }}
              declinePending={declinePending}
              error={cardError}
              blocked={blocked}
              arriving={arrivedCard === card.card_id}
              onApprove={(body: ApproveCard) => {
                reject.reset();
                setEditedCards((current) => toggled(current, card.card_id, carriesEdits(body)));
                approve.mutate({ cardId: card.card_id, body });
              }}
              onDecline={(reason) => {
                approve.reset();
                reject.mutate({ cardId: card.card_id, feedback: reason });
              }}
            />
          ),
        };

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas">
      <ChatHeader title={detail.title} onUnavailable={unavailable} />
      <div ref={scrollRef} onScroll={onScroll} className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto">
        <div ref={contentRef} className="mx-auto w-full max-w-[768px] px-6 pt-6 pb-12">
          <Transcript
            blocks={blocks}
            memberId={memberId}
            working={working}
            showSpark={showSpark}
            failure={detail.turn_failure !== null && !working ? failureLine(detail.turn_failure, budgetState) : null}
            streaming={streaming}
            pending={pending}
            editedCards={editedCards}
            answering={resolve.isPending ? resolve.variables.escalationId : null}
            answerErrors={answerErrors}
            blocked={blocked}
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
      <div className="relative mx-auto w-full max-w-[800px] shrink-0 px-gutter pb-3">
        {!atBottom && !(card !== null && cardInView) && (
          // Not while a waiting card shows: it ends the chat, and its sticky foot sits
          // where the button would.
          <div className="pointer-events-none absolute inset-x-0 -top-12 z-10 flex h-12 items-start justify-center bg-linear-to-t from-canvas">
            <button
              type="button"
              aria-label="Scroll to the latest message"
              onClick={() => {
                toBottom("smooth");
              }}
              className="pointer-events-auto inline-flex size-8 items-center justify-center rounded-full border border-border bg-surface text-secondary shadow-card transition-colors hover:bg-hover animate-fade-in"
            >
              <ArrowDown size={16} strokeWidth={1.8} />
            </button>
          </div>
        )}
        {showBanner && (
          <ComposerBanner
            onClose={() => {
              setBannerClosed(true);
            }}
          >
            Responses from the network will appear here as they arrive.
          </ComposerBanner>
        )}
        {card !== null && cardDraft !== null && !cardInView && (
          <button
            type="button"
            onClick={() => {
              review(card.card_id);
            }}
            className="mx-2.5 -mb-3 flex w-[calc(100%-20px)] items-center gap-2 rounded-t-[14px] bg-bubble pt-2 pr-3 pb-5 pl-4 text-left text-ui text-secondary transition-colors hover:text-ink animate-fade-in"
          >
            <span className="min-w-0 flex-1 truncate">{outreachTitle(cardDraft)}</span>
            <span className="shrink-0 rounded-control bg-primary px-2.5 py-1 text-ui-sm font-medium text-on-primary">
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
          budgetStrip={card === null}
          placeholder={card === null ? "Reply…" : "What should Claude do differently?"}
          onUnavailable={unavailable}
          sendMode={detail.send_mode}
          onSendModeChange={(override) => {
            setSendMode.mutate(override);
          }}
        />
        {post.isError && (
          <p role="alert" className="mt-2 text-center text-ui-sm text-danger">
            {describeFailure(post.error)}
          </p>
        )}
        {setSendMode.isError && (
          <p role="alert" className="mt-2 text-center text-ui-sm text-danger">
            Couldn&apos;t change how this chat sends requests: {describeFailure(setSendMode.error)}
          </p>
        )}
        <p className="mt-2 text-center text-ui-xs text-muted">{composerCaption(card !== null, blocked !== null)}</p>
      </div>
    </div>
  );
}

/** Under the composer: while a card waits, what a message does to it. */
function composerCaption(cardWaiting: boolean, blocked: boolean): string {
  if (!cardWaiting) return "Claude is AI and can make mistakes. Please double-check responses.";
  return blocked
    ? "Claude can't redraft until the budget is back. Ticking and changing the wording still work."
    : "Claude redrafts the request from what you send. Nothing goes out until you approve.";
}

/** `set` with `key` in it or not. */
function toggled(set: ReadonlySet<string>, key: string, present: boolean): ReadonlySet<string> {
  const next = new Set(set);
  if (present) next.add(key);
  else next.delete(key);
  return next;
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
      <p className="max-w-sm text-ui text-muted">{body}</p>
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
      <WhoseNotFound kind="chat" id={conversationId} />
    ) : (
      <ChatMessage title="Couldn't open this chat" body={describeFailure(conversation.error)} />
    );
  }
  return <div className="h-full bg-canvas" aria-busy="true" />;
}
