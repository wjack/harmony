/**
 * The detail pane for one thread (ARCHITECTURE §12 `/requests/:threadId`): the title and
 * routing line, the transcript with our side's rows and an open item's decision card at
 * its end, internal notes, the decision's actions at the bottom edge, and the
 * properties rail.
 */
import { ArrowLeft, ChevronRight, CircleCheck, Eye, Hourglass } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError } from "../api/client";
import { Button, IconButton, OrgAvatar, Spark, Tooltip, cx } from "../shell/ui";
import { EscalationPanel } from "./EscalationPanel";
import { NoteComposer } from "./NoteComposer";
import { PropertiesRail, PropertyStrip } from "./PropertiesRail";
import { RepresentativeSaw } from "./RepresentativeSaw";
import { RoutingLine } from "./RoutingLine";
import { ShareDialog } from "./ShareDialog";
import { Transcript } from "./Transcript";
import { buildActivity } from "./activity";
import { useOrgMembers, useThreadDetail, useWorkspace } from "./api";
import { possessive, representativeName } from "./format";
import { useTyping } from "./live";
import { propertiesOf } from "./properties";
import { buildTimeline, openEscalation, partiesOf } from "./timeline";
import type { InboxItem, OrgRef, ThreadDetail } from "./types";

interface ThreadPaneProps {
  threadId: string;
  memberId: string | undefined;
  /** The thread's inbox row, for who shared it and why. */
  item: InboxItem | undefined;
  onBack?: () => void;
  onSeen: (threadId: string, at: string) => void;
}

/** How the thread ended, with our representative's own summary when it wrote one. */
function EndedBanner({ state, summary, us }: { state: ThreadDetail["state"]; summary: string | null; us: OrgRef }) {
  const closed = state === "closed";
  const Icon = closed ? CircleCheck : Hourglass;
  return (
    <section
      aria-label={closed ? "Closed" : "Expired"}
      className={cx(
        "mt-6 flex items-start gap-3 rounded-[14px] border border-border px-4 py-3",
        closed ? "bg-success-soft" : "bg-surface",
      )}
    >
      <Icon size={20} strokeWidth={1.6} aria-hidden className={cx("mt-px shrink-0", closed ? "text-success" : "text-muted")} />
      <div className="min-w-0 flex-1">
        <p className="text-[14px] font-medium text-ink">{closed ? "Closed" : "Expired"}</p>
        {summary !== null && summary !== "" ? (
          <>
            <p className="mt-0.5 text-[13.5px] leading-[1.5] whitespace-pre-wrap text-secondary">{summary}</p>
            <p className="mt-1 text-[12px] text-muted">
              {possessive(representativeName(us.name))} summary. Only {us.name} sees it.
            </p>
          </>
        ) : (
          <p className="text-[12.5px] text-secondary">
            {closed
              ? "The conversation is over. No summary was written for this side."
              : "It reached its turn limit or deadline before either side closed it."}
          </p>
        )}
      </div>
    </section>
  );
}

export function ThreadPane({ threadId, memberId, item, onBack, onSeen }: ThreadPaneProps) {
  const detail = useThreadDetail(threadId);
  const workspace = useWorkspace(threadId);
  const thread = detail.data;
  const parties = thread === undefined ? null : partiesOf(thread);
  const members = useOrgMembers(parties?.us.id);
  const typing = useTyping(threadId);

  const [shareOpen, setShareOpen] = useState(false);
  const [sawOpen, setSawOpen] = useState(false);
  const [reassignOpen, setReassignOpen] = useState(false);
  const [actionsSlot, setActionsSlot] = useState<HTMLDivElement | null>(null);

  const entries = useMemo(
    () => (thread === undefined ? [] : buildTimeline(thread, workspace.data)),
    [thread, workspace.data],
  );
  const escalation = workspace.data === undefined ? null : openEscalation(workspace.data.escalations, memberId);

  useEffect(() => {
    if (thread !== undefined) onSeen(thread.id, thread.last_activity_at);
  }, [thread, onSeen]);

  // Open at the latest message, or at the top of a decision taller than the pane; then
  // follow new entries only while the reader is at the bottom, so scrolling back
  // through the thread isn't yanked away.
  const scroller = useRef<HTMLDivElement>(null);
  const decision = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useLayoutEffect(() => {
    pinned.current = true;
  }, [threadId]);
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element === null || !pinned.current) return;
    element.scrollTop = element.scrollHeight;
    const card = decision.current;
    if (card === null) return;
    const above = card.getBoundingClientRect().top - element.getBoundingClientRect().top;
    if (above < 0) element.scrollTop += above - 16;
  }, [entries.length, typing, threadId, escalation?.id]);

  if (detail.isPending) return <PaneSkeleton onBack={onBack} />;
  if (detail.isError || thread === undefined || parties === null) {
    const hidden = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <PaneMessage
        onBack={onBack}
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
  const eyebrow = thread.side === "responder" ? `Request from ${them.name}` : `Our request to ${them.name}`;
  const orgMembers = members.data ?? [];
  const properties = propertiesOf(thread, escalation, memberId);
  const activity = workspace.data === undefined ? [] : buildActivity(workspace.data, us);
  const representative = workspace.data?.representative ?? null;
  const consult =
    escalation?.consult_id == null
      ? null
      : (workspace.data?.consults.find((candidate) => candidate.id === escalation.consult_id) ?? null);
  const ended = thread.state === "closed" || thread.state === "expired";
  const sharedBy =
    item?.shared_by == null ? null : { name: item.shared_by.name, question: item.shared_question };

  return (
    <div className="@container/thread flex min-h-0 min-w-0 flex-1 flex-col">
      <header className="flex h-[52px] shrink-0 items-center gap-2 pr-3 pl-4">
        {onBack !== undefined && (
          <IconButton label="Back to requests" size="sm" className="@2xl/inbox:hidden" onClick={onBack}>
            <ArrowLeft size={16} strokeWidth={1.5} />
          </IconButton>
        )}
        <OrgAvatar name={them.name} seed={them.slug} size={20} />
        <span className="hidden max-w-[220px] truncate text-[13.5px] text-muted @xl/thread:block">{them.name}</span>
        <ChevronRight size={14} strokeWidth={1.5} aria-hidden className="hidden shrink-0 text-faint @xl/thread:block" />
        <span className="min-w-0 flex-1 truncate text-[13.5px] text-ink">{thread.title}</span>
        <Tooltip content="What our representative saw" side="bottom">
          <IconButton
            label="What our representative saw"
            size="sm"
            onClick={() => {
              setSawOpen(true);
            }}
          >
            <Eye size={16} strokeWidth={1.5} />
          </IconButton>
        </Tooltip>
        <Button
          size="sm"
          variant="secondary"
          onClick={() => {
            setShareOpen(true);
          }}
        >
          Share
        </Button>
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          <div
            ref={scroller}
            onScroll={(event) => {
              const element = event.currentTarget;
              pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
            }}
            className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto"
          >
            <div className="mx-auto w-full max-w-[760px] px-5 pt-5 pb-10 @xl/thread:px-8">
              {/* A responder's title is "Request from …" until the requester has written. */}
              {eyebrow !== thread.title && <p className="mb-1.5 text-[12.5px] text-muted">{eyebrow}</p>}
              <h1 className="font-serif text-[27px] leading-[1.22] tracking-[-0.015em] text-ink">{thread.title}</h1>
              <div className="mt-2.5 empty:hidden">
                <RoutingLine
                  escalation={escalation}
                  memberId={memberId}
                  sharedBy={sharedBy}
                  onReassign={() => {
                    setReassignOpen(true);
                  }}
                />
              </div>
              <div className="mt-3.5 @min-[820px]/thread:hidden">
                <PropertyStrip
                  properties={properties}
                  onShowSaw={() => {
                    setSawOpen(true);
                  }}
                />
              </div>

              <hr className="my-7 border-border" />

              {entries.length === 0 && typing === null ? (
                <p className="py-6 text-center text-[13px] text-muted">No messages yet.</p>
              ) : (
                <Transcript entries={entries} us={us} them={them} typing={typing} />
              )}

              {escalation !== null && (
                <div ref={decision} className="mt-6">
                  <EscalationPanel
                    key={escalation.id}
                    escalation={escalation}
                    consult={consult}
                    threadId={thread.id}
                    us={us}
                    them={them}
                    members={orgMembers}
                    memberId={memberId}
                    reassignOpen={reassignOpen}
                    onReassignOpen={setReassignOpen}
                    actionsSlot={actionsSlot}
                  />
                </div>
              )}

              {ended && <EndedBanner state={thread.state} summary={workspace.data?.close_summary ?? null} us={us} />}

              <div className="mt-8">
                <NoteComposer threadId={thread.id} us={us} members={orgMembers} memberId={memberId} />
              </div>
            </div>
          </div>

          {escalation !== null && (
            // The decision's actions, in reach wherever the transcript is scrolled.
            <div ref={setActionsSlot} className="shrink-0 px-4 pt-2 pb-4 @xl/thread:px-8" />
          )}
        </div>

        <aside
          aria-label="Properties"
          className="scrollbar-quiet hidden w-[296px] shrink-0 overflow-y-auto pt-5 pr-4 pb-6 @min-[820px]/thread:block @5xl/thread:w-[316px]"
        >
          <PropertiesRail
            properties={properties}
            them={them}
            us={us}
            representative={representative}
            activity={activity}
            onShowSaw={() => {
              setSawOpen(true);
            }}
          />
        </aside>
      </div>

      <ShareDialog
        open={shareOpen}
        onOpenChange={setShareOpen}
        threadId={thread.id}
        us={us}
        members={orgMembers}
        access={thread.access}
        memberId={memberId}
      />
      <RepresentativeSaw
        open={sawOpen}
        onOpenChange={setSawOpen}
        us={us}
        them={them}
        representative={representative}
      />
    </div>
  );
}

function PaneSkeleton({ onBack }: { onBack: (() => void) | undefined }) {
  return (
    <div className="flex min-w-0 flex-1 flex-col" aria-busy="true" aria-label="Loading thread">
      <header className="flex h-[52px] items-center gap-2 px-4">
        {onBack !== undefined && (
          <IconButton label="Back to requests" size="sm" className="@2xl/inbox:hidden" onClick={onBack}>
            <ArrowLeft size={16} strokeWidth={1.5} />
          </IconButton>
        )}
        <span className="h-4 w-48 animate-pulse rounded bg-hover" />
      </header>
      <div className="mx-auto w-full max-w-[760px] px-8 pt-5">
        <span className="block h-8 w-2/3 animate-pulse rounded bg-hover" />
        <span className="mt-3 block h-4 w-1/2 animate-pulse rounded bg-hover" />
        <div className="mt-10 flex flex-col gap-6">
          {[0.62, 0.48, 0.7].map((width, index) => (
            <span
              key={width}
              className={cx("block h-16 animate-pulse rounded-[18px] bg-hover", index % 2 === 1 && "self-end")}
              style={{ width: `${String(width * 100)}%` }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function PaneMessage({
  title,
  body,
  action,
  onBack,
}: {
  title: string;
  body: string;
  action?: ReactNode;
  onBack: (() => void) | undefined;
}) {
  return (
    <div className="flex min-w-0 flex-1 flex-col">
      {onBack !== undefined && (
        <header className="flex h-[52px] items-center px-4 @2xl/inbox:hidden">
          <IconButton label="Back to requests" size="sm" onClick={onBack}>
            <ArrowLeft size={16} strokeWidth={1.5} />
          </IconButton>
        </header>
      )}
      <div className="flex flex-1 flex-col items-center justify-center px-8 text-center">
        <Spark size={24} />
        <p className="mt-3 font-serif text-[20px] text-ink">{title}</p>
        <p className="mt-1.5 max-w-sm text-[13.5px] leading-5 text-muted">{body}</p>
        {action !== undefined && <div className="mt-4">{action}</div>}
      </div>
    </div>
  );
}
