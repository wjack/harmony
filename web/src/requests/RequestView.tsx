/**
 * One request, full width (LIVE_DEMO_PLAN 3.9): a header with the short title, the
 * counterparty, a status pill and Details; the transcript in the chat's column, with an
 * open item's decision card after its last row; and one box below, as in a chat, for a
 * comment to the team or an Edit to our Claude. Details opens beside the transcript, or
 * over it as a sheet when the view is narrow.
 */
import { ArrowDown, PanelRight } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type Ref } from "react";
import { useNavigate } from "react-router";

import { ApiError } from "../api/client";
import { useSession } from "../app/queries";
import { useBudgetState } from "../chat/failureCopy";
import { Pill } from "../chat/parts";
import { useStickToBottom } from "../chat/useStickToBottom";
import { WhoseNotFound } from "../demo/WhoseNotFound";
import { Button, Spark, cx } from "../shell/ui";
import { DecisionCard } from "./DecisionCard";
import { DeclinedTurnCard } from "./DeclinedTurnCard";
import { Details } from "./Details";
import type { RowActions } from "./EventRows";
import { RepresentativeSaw } from "./RepresentativeSaw";
import { RequestComposer } from "./RequestComposer";
import { ShareDialog } from "./ShareDialog";
import { Transcript } from "./Transcript";
import { buildActivity } from "./activity";
import { useOrgMembers, useThreadDetail, useWorkspace } from "./api";
import { useDetailsOpen } from "./detailsOpen";
import { isTerminal } from "./format";
import { useTyping, type Typing } from "./live";
import { propertiesOf, type RequestStatus } from "./properties";
import { useSeen } from "./seen";
import { buildTimeline, openEscalation, partiesOf } from "./timeline";
import type { MemberView, OrgRef, ThreadDetail, ThreadWorkspace } from "./types";
import { useComposer } from "./useComposer";
import { useDecision } from "./useDecision";
import { useDeclinedTurn } from "./useDeclinedTurn";
import { useReopen } from "./useReopen";
import { waitingLine } from "./waiting";

/** Why the box opened in Edit from a declined turn's card. */
const RETRY_WITH_EDIT = "Claude tries its turn again with this when you send it.";

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
  const viewer = useSession().data?.member;
  const memberId = viewer?.id;
  const detail = useThreadDetail(threadId);
  const workspace = useWorkspace(threadId);
  const thread = detail.data;
  const parties = thread === undefined ? null : partiesOf(thread);
  const members = useOrgMembers(parties?.us.id);
  const typing = useTyping(threadId, parties?.us.id);
  const { markSeen } = useSeen(memberId);
  const { ref, narrow } = useNarrowerThan(DETAILS_BESIDE_PX);
  const [besideOpen, setBesideOpen] = useDetailsOpen(memberId);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [sawOpen, setSawOpen] = useState(false);
  /** Bumped by See guidance: Details opens at its guidance section. */
  const [guidanceRequest, setGuidanceRequest] = useState(0);
  const detailsButton = useRef<HTMLButtonElement | null>(null);
  /** The Details control that opened Share or what our representative saw. */
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (thread !== undefined) markSeen(thread.id, thread.last_activity_at);
  }, [thread, markSeen]);

  if (detail.isPending) return <ViewSkeleton />;
  if (detail.isError || thread === undefined || parties === null) {
    const hidden = detail.error instanceof ApiError && detail.error.status === 404;
    // Seen until now: a reassign or unshare took away the viewer's access.
    if (hidden && thread !== undefined) return <NoLongerVisible />;
    // Someone else's, or from an earlier demo world: the page says whose, with a switch to them.
    if (hidden) return <WhoseNotFound kind="thread" id={threadId} />;
    return (
      <ViewMessage
        title="Couldn't load this request"
        body={detail.error?.message ?? "Try again in a moment."}
        action={
          <Button
            size="sm"
            onClick={() => {
              void detail.refetch();
            }}
          >
            Retry
          </Button>
        }
      />
    );
  }

  const { us, them } = parties;
  // An ended thread's last turn writes only our summary, which the Closed row's "still
  // wrapping up" covers: a writing row would read as another message coming.
  const writing = isTerminal(thread.state) ? null : typing;
  const orgMembers = members.data ?? [];
  const escalation = workspace.data === undefined ? null : openEscalation(workspace.data.escalations, memberId);
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
  const seeGuidance = () => {
    if (narrow) setSheetOpen(true);
    else setBesideOpen(true);
    setGuidanceRequest((count) => count + 1);
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
          workspace={workspace.data}
          typing={writing}
          us={us}
          them={them}
          members={orgMembers}
          memberId={memberId}
          admin={viewer?.role === "admin"}
          onSeeGuidance={seeGuidance}
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
        guidance={workspace.data?.guidance ?? null}
        guidanceRequest={guidanceRequest}
        activity={workspace.data === undefined ? [] : buildActivity(workspace.data)}
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
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-border pr-3 pl-gutter">
      {/* The status keeps its width; the counterparty gives way first, then the title truncates. */}
      <div className="flex min-w-0 flex-1 items-center gap-2 text-ui">
        <h1 className="min-w-0 truncate font-medium text-ink">{title}</h1>
        {!named && (
          <span className="hidden min-w-0 shrink-[4] truncate whitespace-nowrap text-muted @lg/view:inline">
            with {them.name}
          </span>
        )}
        <Pill tone={status.tone} pulse={status.needs} title={status.label}>
          {status.label}
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
  workspace: ThreadWorkspace | undefined;
  typing: Typing | null;
  us: OrgRef;
  them: OrgRef;
  members: readonly MemberView[];
  memberId: string | undefined;
  admin: boolean;
  onSeeGuidance: () => void;
}

/** Never keyed by item: the box keeps its text while items open and resolve. */
function Conversation(props: ConversationProps) {
  const { thread, workspace, typing, us, them, members, memberId, admin, onSeeGuidance } = props;
  const budget = useBudgetState();
  const composer = useComposer();
  const steers = workspace?.steers ?? [];
  const open = workspace === undefined ? null : openEscalation(workspace.escalations, memberId);
  // A turn Claude declined has no draft to decide: its own card, and Edit retries it.
  const declined = useDeclinedTurn(open?.kind === "refused_turn" ? open : null, thread.id);
  const escalation = declined === null ? open : null;
  const consult =
    escalation?.consult_id == null
      ? null
      : (workspace?.consults.find((candidate) => candidate.id === escalation.consult_id) ?? null);
  const decision = useDecision(escalation, consult, steers, thread.id, us, them);
  const reopen = useReopen(thread.id, { us: us.name, them: them.name }, composer.toEdit);
  const entries = useMemo(() => buildTimeline(thread, workspace), [thread, workspace]);
  const closed = isTerminal(thread.state);
  // An Edit typed but not sent when the conversation ends can't reach Claude from the
  // box, and sent from there it would be a comment no Claude reads. Reopen is how Claude
  // takes it now, so it moves to that prompt; where this side can't reopen it stays in
  // the box, as a comment the member can see is one.
  useEffect(() => {
    if (!closed || composer.mode !== "edit") return;
    const latest = entries.findLast((entry) => entry.type === "ended" && entry.latest);
    const reopenable = thread.reopen !== null && thread.reopen.state !== "blocked";
    if (composer.text.trim() !== "" && latest !== undefined && reopenable) {
      reopen.open(latest.id, composer.text);
      composer.setText("");
    }
    composer.setMode("comment");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only the close moves the text
  }, [closed]);
  const waiting = waitingLine({
    thread,
    typing: typing !== null,
    consults: workspace?.consults ?? [],
    escalation,
    viewerId: memberId,
    us,
    them,
  });
  const actions: RowActions = {
    side: thread.side,
    budget,
    hasGuidance: workspace?.guidance != null,
    onSeeGuidance,
    onRetryEdit: (text) => {
      composer.toEdit(text);
    },
    reopen,
    reopenOffer: thread.reopen,
  };
  const running = steers.find((steer) => steer.kind === "edit" && steer.status === "working") ?? null;
  const unsentEdit = decision !== null && composer.mode === "edit" && composer.text.trim() !== "";
  const { scrollRef, contentRef, atBottom, onScroll, toBottom, revealTop } = useStickToBottom([
    entries.length,
    typing?.text,
    typing === null,
    waiting,
    reopen.prompt?.anchor,
  ]);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const cardFor = decision?.escalation.id ?? declined?.escalation.id ?? null;
  // A card taller than the view would open at its end; its question comes first.
  useLayoutEffect(() => {
    if (cardFor !== null && cardRef.current !== null) revealTop(cardRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a new item moves the view
  }, [cardFor]);

  return (
    <>
      <div ref={scrollRef} onScroll={onScroll} className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto">
        <div ref={contentRef} className="mx-auto w-full max-w-[800px] px-gutter pt-6 pb-10">
          {entries.length === 0 && typing === null && waiting === null ? (
            <p className="py-6 text-center text-ui-sm text-muted">No messages yet.</p>
          ) : (
            <Transcript entries={entries} us={us} them={them} typing={typing} waiting={waiting} actions={actions} />
          )}
          {declined !== null && (
            <div ref={cardRef} className="mt-8">
              <DeclinedTurnCard
                turn={declined}
                threadId={thread.id}
                us={us}
                them={them}
                members={members}
                memberId={memberId}
                onEdit={() => {
                  composer.toEdit(composer.mode === "edit" ? composer.text : "", RETRY_WITH_EDIT);
                }}
                unsentEdit={composer.mode === "edit" && composer.text.trim() !== ""}
              />
            </div>
          )}
          {decision !== null && (
            <div ref={cardRef} className="mt-8">
              <DecisionCard
                decision={decision}
                threadId={thread.id}
                us={us}
                them={them}
                members={members}
                memberId={memberId}
                admin={admin}
                unsentEdit={unsentEdit}
              />
            </div>
          )}
        </div>
      </div>
      <div className="relative mx-auto w-full max-w-[800px] shrink-0 px-gutter pb-3">
        {!atBottom && (
          <div className="pointer-events-none absolute inset-x-0 -top-12 flex h-12 items-center justify-center bg-linear-to-t from-canvas">
            <button
              type="button"
              aria-label="Scroll to the latest message"
              onClick={() => {
                toBottom("smooth");
              }}
              className="pointer-events-auto inline-flex size-8 animate-fade-in items-center justify-center rounded-full border border-border bg-surface text-secondary shadow-card transition-colors hover:bg-hover"
            >
              <ArrowDown size={16} strokeWidth={1.8} />
            </button>
          </div>
        )}
        <RequestComposer
          threadId={thread.id}
          us={us}
          members={members}
          memberId={memberId}
          composer={composer}
          decision={decision}
          closed={closed}
          running={running}
          onEditSent={declined?.retry}
        />
      </div>
    </>
  );
}

function ViewSkeleton() {
  return (
    <div className="flex h-full min-w-0 flex-col" aria-busy="true" aria-label="Loading thread">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-4">
        <span className="h-4 w-48 animate-pulse rounded bg-hover" />
        <span className="h-5 w-24 animate-pulse rounded-full bg-hover" />
      </header>
      <div className="mx-auto flex w-full max-w-[800px] flex-col gap-7 px-gutter pt-6">
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
      <p className="mt-1.5 max-w-sm text-ui-sm text-muted">{body}</p>
      {action !== undefined && <div className="mt-4">{action}</div>}
    </div>
  );
}
