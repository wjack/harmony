/**
 * One request, full width (LIVE_DEMO_PLAN 3.9): a header with the short title, the
 * counterparty, a status pill and Details; the transcript in the chat's 768px column,
 * with an open item's decision card after its last row; and one composer below, as in
 * a chat. Details opens beside the transcript, or over it as a sheet when the view is
 * narrow.
 */
import { ArrowDown, CircleCheck, Hourglass, OctagonPause, PanelRight } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type Ref } from "react";
import { useNavigate } from "react-router";

import { ApiError } from "../api/client";
import { useBudget, type BudgetState } from "../app/budget";
import { useSession } from "../app/queries";
import { stoppedLine, type FailureReason } from "../chat/failureCopy";
import { Pill } from "../chat/parts";
import { useStickToBottom } from "../chat/useStickToBottom";
import { Button, Spark, cx } from "../shell/ui";
import { DecisionCard } from "./DecisionCard";
import { Details } from "./Details";
import { RepresentativeSaw } from "./RepresentativeSaw";
import { RequestComposer } from "./RequestComposer";
import { ShareDialog } from "./ShareDialog";
import { Transcript } from "./Transcript";
import { buildActivity } from "./activity";
import { useOrgMembers, useThreadDetail, useWorkspace } from "./api";
import { useDetailsOpen } from "./detailsOpen";
import { possessive, representativeName } from "./format";
import { useTyping, type Typing } from "./live";
import { propertiesOf, type RequestStatus } from "./properties";
import { useSeen } from "./seen";
import { buildTimeline, openEscalation, partiesOf, type TimelineEntry } from "./timeline";
import type { ConsultView, EscalationView, MemberView, OrgRef, ThreadDetail } from "./types";
import { useDecision } from "./useDecision";

/** Below this the view has no room for Details beside the transcript. */
const DETAILS_BESIDE_PX = 820;

/** Whether the element is narrower than `px`; until measured (or with no layout), it isn't. */
function useNarrowerThan(px: number) {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const [narrow, setNarrow] = useState(false);
  useLayoutEffect(() => {
    if (element === null) return;
    const measure = () => {
      const { width } = element.getBoundingClientRect();
      if (width > 0) setNarrow(width < px);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, [element, px]);
  return { ref: setElement, narrow };
}

export function RequestView({ threadId }: { threadId: string }) {
  const memberId = useSession().data?.member.id;
  const detail = useThreadDetail(threadId);
  const workspace = useWorkspace(threadId);
  const thread = detail.data;
  const parties = thread === undefined ? null : partiesOf(thread);
  const members = useOrgMembers(parties?.us.id);
  const typing = useTyping(threadId);
  const budget = useBudget().data?.state ?? "ok";
  const { markSeen } = useSeen(memberId);
  const { ref, narrow } = useNarrowerThan(DETAILS_BESIDE_PX);
  const [besideOpen, setBesideOpen] = useDetailsOpen(memberId);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [sawOpen, setSawOpen] = useState(false);
  const detailsButton = useRef<HTMLButtonElement | null>(null);
  /** The Details control that opened Share or what our representative saw. */
  const opener = useRef<HTMLElement | null>(null);

  const entries = useMemo(
    () => (thread === undefined ? [] : buildTimeline(thread, workspace.data)),
    [thread, workspace.data],
  );

  useEffect(() => {
    if (thread !== undefined) markSeen(thread.id, thread.last_activity_at);
  }, [thread, markSeen]);

  if (detail.isPending) return <ViewSkeleton />;
  if (detail.isError || thread === undefined || parties === null) {
    const hidden = detail.error instanceof ApiError && detail.error.status === 404;
    // Seen until now: a reassign or unshare took away the viewer's access.
    if (hidden && thread !== undefined) return <NoLongerVisible />;
    return (
      <ViewMessage
        title={hidden ? "This thread isn't visible to you" : "Couldn't load this thread"}
        body={
          hidden
            ? "Threads are visible to the people they're routed or shared to, and to admins."
            : (detail.error?.message ?? "Try again in a moment.")
        }
        action={
          hidden ? undefined : (
            <Button
              size="sm"
              onClick={() => {
                void detail.refetch();
              }}
            >
              Retry
            </Button>
          )
        }
      />
    );
  }

  const { us, them } = parties;
  const orgMembers = members.data ?? [];
  const escalation = workspace.data === undefined ? null : openEscalation(workspace.data.escalations, memberId);
  const consult =
    escalation?.consult_id == null
      ? null
      : (workspace.data?.consults.find((candidate) => candidate.id === escalation.consult_id) ?? null);
  const properties = propertiesOf(thread, escalation, memberId);
  const representative = workspace.data?.representative ?? null;
  const detailsOpen = narrow ? sheetOpen : besideOpen;
  // None of these opens from a Radix trigger, so each says where focus goes back to.
  const focusDetails = () => {
    detailsButton.current?.focus();
  };
  const focusOpener = () => {
    if (opener.current?.isConnected === true) opener.current.focus();
    else focusDetails();
  };

  return (
    <div ref={ref} className="flex h-full min-h-0 bg-canvas">
      {/* The column, not the whole view, sizes the header: Details takes room beside it. */}
      <div className="@container/view flex min-w-0 flex-1 flex-col">
        <ViewHeader
          title={thread.title}
          them={them}
          status={properties.status}
          detailsOpen={detailsOpen}
          detailsRef={detailsButton}
          onDetails={() => {
            if (narrow) setSheetOpen(!sheetOpen);
            else setBesideOpen(!besideOpen);
          }}
        />
        <Conversation
          thread={thread}
          entries={entries}
          typing={typing}
          escalation={escalation}
          consult={consult}
          closeSummary={workspace.data?.close_summary ?? null}
          budget={budget}
          us={us}
          them={them}
          members={orgMembers}
          memberId={memberId}
        />
      </div>

      <Details
        sheet={narrow}
        open={detailsOpen}
        onOpenChange={narrow ? setSheetOpen : setBesideOpen}
        properties={properties}
        them={them}
        us={us}
        representative={representative}
        activity={workspace.data === undefined ? [] : buildActivity(workspace.data, us)}
        returnFocus={focusDetails}
        onShare={(from) => {
          opener.current = from;
          setShareOpen(true);
        }}
        onShowSaw={(from) => {
          opener.current = from;
          setSawOpen(true);
        }}
      />
      <ShareDialog
        open={shareOpen}
        onOpenChange={setShareOpen}
        threadId={thread.id}
        us={us}
        members={orgMembers}
        access={thread.access}
        memberId={memberId}
        returnFocus={focusOpener}
      />
      <RepresentativeSaw
        open={sawOpen}
        onOpenChange={setSawOpen}
        us={us}
        them={them}
        representative={representative}
        returnFocus={focusOpener}
      />
    </div>
  );
}

interface ViewHeaderProps {
  title: string;
  them: OrgRef;
  status: RequestStatus;
  detailsOpen: boolean;
  detailsRef: Ref<HTMLButtonElement>;
  onDetails: () => void;
}

function ViewHeader({ title, them, status, detailsOpen, detailsRef, onDetails }: ViewHeaderProps) {
  // "Waiting on {them}" already says who the request is with.
  const named = status.label.includes(them.name);
  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-border pr-3 pl-4">
      <div className="flex min-w-0 flex-1 items-center gap-2 text-[14px]">
        {/* The title comes first: it keeps its width, short of room for the status's dot.
            The counterparty gives way, then the status truncates; on a phone the status
            shows as its dot. */}
        <h1 className="max-w-[calc(100%-2.5rem)] shrink-0 truncate font-medium text-ink">{title}</h1>
        {!named && <span className="hidden min-w-0 shrink-[4] truncate text-muted @lg/view:inline">with {them.name}</span>}
        <Pill tone={status.tone} pulse={status.needs} title={status.label} shrinks>
          <span className="sr-only @md/view:not-sr-only @md/view:min-w-0">
            <span className="block truncate">{status.label}</span>
          </span>
        </Pill>
      </div>
      <Button
        ref={detailsRef}
        size="sm"
        variant="ghost"
        aria-label="Details"
        aria-expanded={detailsOpen}
        icon={<PanelRight size={16} strokeWidth={1.5} />}
        onClick={onDetails}
        className={cx(detailsOpen && "bg-hover")}
      >
        <span className="hidden @md/view:inline">Details</span>
      </Button>
    </header>
  );
}

interface ConversationProps {
  thread: ThreadDetail;
  entries: readonly TimelineEntry[];
  typing: Typing | null;
  escalation: EscalationView | null;
  consult: ConsultView | null;
  closeSummary: string | null;
  budget: BudgetState;
  us: OrgRef;
  them: OrgRef;
  members: readonly MemberView[];
  memberId: string | undefined;
}

/** Never keyed by item: the composer keeps its text while items open and resolve. */
function Conversation(props: ConversationProps) {
  const { thread, escalation, consult, closeSummary, budget, us, them, members, memberId } = props;
  const decision = useDecision(escalation, consult, thread.id, us, them);
  let banner: ReactNode = null;
  if (thread.stop_reason != null) {
    banner = <StoppedBanner reason={thread.stop_reason} budget={budget} summary={closeSummary} us={us} />;
  }
  else if (thread.state === "closed" || thread.state === "expired") {
    banner = <EndedBanner state={thread.state} summary={closeSummary} us={us} />;
  }
  return (
    <Frame
      {...props}
      banner={banner}
      cardFor={decision?.escalation.id ?? null}
      card={
        decision && (
          <DecisionCard decision={decision} threadId={thread.id} us={us} them={them} members={members} memberId={memberId} />
        )
      }
    >
      <RequestComposer threadId={thread.id} us={us} them={them} members={members} memberId={memberId} decision={decision} />
    </Frame>
  );
}

interface FrameProps {
  entries: readonly TimelineEntry[];
  typing: Typing | null;
  us: OrgRef;
  them: OrgRef;
  card: ReactNode;
  /** The item the card is for: a new one is read from its top. */
  cardFor: string | null;
  banner: ReactNode;
  /** The composer. */
  children: ReactNode;
}

/** The chat's frame: the transcript pinned to its end while the reader is there, then the composer. */
function Frame({ entries, typing, us, them, card, cardFor, banner, children }: FrameProps) {
  const { scrollRef, contentRef, atBottom, onScroll, toBottom, revealTop } = useStickToBottom([
    entries.length,
    typing?.text,
    typing === null,
  ]);
  const cardRef = useRef<HTMLDivElement | null>(null);
  // A card taller than the view would open at its end; its question comes first.
  useLayoutEffect(() => {
    if (cardFor !== null && cardRef.current !== null) revealTop(cardRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a new item moves the view
  }, [cardFor]);
  return (
    <>
      <div ref={scrollRef} onScroll={onScroll} className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto">
        <div ref={contentRef} className="mx-auto w-full max-w-[768px] px-5 pt-6 pb-10 sm:px-6">
          {entries.length === 0 && typing === null ? (
            <p className="py-6 text-center text-[13px] text-muted">No messages yet.</p>
          ) : (
            <Transcript entries={entries} us={us} them={them} typing={typing} />
          )}
          {card !== null && (
            <div ref={cardRef} className="mt-8">
              {card}
            </div>
          )}
          {banner}
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
            className="absolute -top-12 left-1/2 z-10 inline-flex size-8 -translate-x-1/2 animate-fade-in items-center justify-center rounded-full border border-border bg-surface text-secondary shadow-card transition-colors hover:bg-hover"
          >
            <ArrowDown size={16} strokeWidth={1.8} />
          </button>
        )}
        {children}
      </div>
    </>
  );
}

/** How the thread ended, with our representative's own summary when it wrote one. */
function EndedBanner({ state, summary, us }: { state: ThreadDetail["state"]; summary: string | null; us: OrgRef }) {
  const closed = state === "closed";
  const Icon = closed ? CircleCheck : Hourglass;
  return (
    <section
      aria-label={closed ? "Closed" : "Expired"}
      className={cx(
        "mt-8 flex items-start gap-3 rounded-[14px] border border-border px-4 py-3",
        closed ? "bg-success-soft" : "bg-surface",
      )}
    >
      <Icon size={20} strokeWidth={1.6} aria-hidden className={cx("mt-px shrink-0", closed ? "text-success" : "text-muted")} />
      <div className="min-w-0 flex-1">
        <p className="text-[14px] font-medium text-ink">{closed ? "Closed" : "Expired"}</p>
        {summary !== null && summary !== "" ? (
          <SideSummary summary={summary} us={us} />
        ) : (
          <p className="text-[12.5px] text-secondary">
            {closed
              ? "The conversation is over. No summary was written for this side."
              : "It ended without a close: both sides went quiet, or it reached its turn limit or deadline."}
          </p>
        )}
      </div>
    </section>
  );
}

/** Our representative's closing summary, which only our side sees. */
function SideSummary({ summary, us }: { summary: string; us: OrgRef }) {
  return (
    <>
      <p className="mt-0.5 text-[13.5px] leading-[1.5] whitespace-pre-wrap text-secondary">{summary}</p>
      <p className="mt-1 text-[12px] text-muted">
        {possessive(representativeName(us.name))} summary. Only {us.name} sees it.
      </p>
    </>
  );
}

/**
 * The system, not a person, ended the thread: why, given what the budget says now, and
 * our summary when our representative wrote one in its last turn (the other side refused).
 */
function StoppedBanner({
  reason,
  budget,
  summary,
  us,
}: {
  reason: FailureReason;
  budget: BudgetState;
  summary: string | null;
  us: OrgRef;
}) {
  return (
    <section aria-label="Stopped" className="mt-8 flex items-start gap-3 rounded-[14px] border border-border bg-surface px-4 py-3">
      <OctagonPause size={20} strokeWidth={1.6} aria-hidden className="mt-px shrink-0 text-warning" />
      <div className="min-w-0 flex-1">
        <p className="text-[14px] font-medium text-ink">Stopped</p>
        <p className="mt-0.5 text-[13.5px] leading-[1.5] text-secondary">{stoppedLine(reason, budget)}</p>
        {summary !== null && summary !== "" && <SideSummary summary={summary} us={us} />}
      </div>
    </section>
  );
}

function ViewSkeleton() {
  return (
    <div className="flex h-full min-w-0 flex-col" aria-busy="true" aria-label="Loading thread">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-4">
        <span className="h-4 w-48 animate-pulse rounded bg-hover" />
        <span className="h-5 w-24 animate-pulse rounded-full bg-hover" />
      </header>
      <div className="mx-auto flex w-full max-w-[768px] flex-col gap-7 px-6 pt-6">
        {[0.82, 0.64, 0.74].map((width) => (
          <div key={width} className="flex gap-3">
            <span className="size-7 shrink-0 animate-pulse rounded-[7px] bg-hover" />
            <span className="flex flex-1 flex-col gap-2 pt-1">
              <span className="h-3 w-40 animate-pulse rounded bg-hover" />
              <span className="h-3 animate-pulse rounded bg-hover" style={{ width: `${String(width * 100)}%` }} />
              <span className="h-3 w-1/2 animate-pulse rounded bg-hover" />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** A request the viewer could see until a moment ago, as their own reassign can leave it. */
function NoLongerVisible() {
  const navigate = useNavigate();
  const back = useRef<HTMLButtonElement | null>(null);
  // What had focus went with the view: keep the keyboard on the way out.
  useEffect(() => {
    const active = document.activeElement;
    if (active === null || active === document.body) back.current?.focus();
  }, []);
  return (
    <ViewMessage
      title="This request is no longer visible to you"
      body="It was reassigned or stopped being shared with you, so it has left your Requests."
      action={
        <Button
          ref={back}
          size="sm"
          onClick={() => {
            void navigate("/requests");
          }}
        >
          Back to Requests
        </Button>
      }
    />
  );
}

function ViewMessage({ title, body, action }: { title: string; body: string; action?: ReactNode }) {
  return (
    <div className="flex h-full min-w-0 flex-col items-center justify-center px-8 text-center">
      <Spark size={24} />
      <p className="mt-3 font-serif text-[20px] text-ink">{title}</p>
      <p className="mt-1.5 max-w-sm text-[13.5px] leading-5 text-muted">{body}</p>
      {action !== undefined && <div className="mt-4">{action}</div>}
    </div>
  );
}
