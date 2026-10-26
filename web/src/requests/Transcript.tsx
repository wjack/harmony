/**
 * The thread between the two representatives (UI_REFERENCE §7): the counterparty's
 * messages on the left, ours on the right, and our side's consults, releases and
 * internal notes inline as rows only our org sees.
 */
import { ArrowUpFromLine, Check, ChevronRight, Lock, MessageCircleQuestion, UserRound, X } from "lucide-react";
import { useState, type ReactNode } from "react";

import { useCalendar } from "../demo/worldClock";
import { firstName } from "../shell/names";
import { Avatar, OrgAvatar, Spark, Tooltip, cx } from "../shell/ui";
import { clockTime, dateTime, representativeName } from "./format";
import type { Typing } from "./live";
import type {
  CloseEntry,
  ConsultEntry,
  MessageEntry,
  NoteEntry,
  ReleaseEntry,
  ResolutionEntry,
  TimelineEntry,
} from "./timeline";
import type { EscalationAction, OrgRef } from "./types";

interface TranscriptProps {
  entries: readonly TimelineEntry[];
  us: OrgRef;
  them: OrgRef;
  typing: Typing | null;
}

export function Transcript({ entries, us, them, typing }: TranscriptProps) {
  return (
    <ol aria-label="Thread" className="flex flex-col gap-5">
      {entries.map((entry) => (
        <li key={entry.id}>
          <Entry entry={entry} us={us} them={them} />
        </li>
      ))}
      {typing !== null && (
        <li>
          <TypingRow typing={typing} us={us} them={them} />
        </li>
      )}
    </ol>
  );
}

function Entry({ entry, us, them }: { entry: TimelineEntry; us: OrgRef; them: OrgRef }) {
  switch (entry.type) {
    case "message":
      return <MessageRow entry={entry} />;
    case "close":
      return <CloseRow entry={entry} />;
    case "consult":
      return <ConsultRow entry={entry} us={us} />;
    case "release":
      return <ReleaseRow entry={entry} us={us} />;
    case "resolution":
      return <ResolutionRow entry={entry} us={us} them={them} />;
    case "note":
      return <NoteRow entry={entry} us={us} />;
  }
}

// --- Messages ------------------------------------------------------------------------------

function provenanceLabel(entry: MessageEntry): string | null {
  switch (entry.provenance) {
    case "agent":
      return null;
    case "agent_approved":
      return entry.approvedBy === null ? "Approved by a teammate" : `Approved by ${firstName(entry.approvedBy.name)}`;
    case "human":
      return entry.approvedBy === null ? "Written by a teammate" : `Written by ${firstName(entry.approvedBy.name)}`;
  }
}

export function MessageRow({ entry }: { entry: MessageEntry }) {
  const calendar = useCalendar();
  const provenance = provenanceLabel(entry);
  const author = entry.provenance === "human" && entry.approvedBy !== null ? entry.approvedBy.name : representativeName(entry.org.name);

  return (
    <article
      aria-label={`${author}, ${clockTime(entry.at)}`}
      className={cx("flex gap-2.5", entry.ours ? "flex-row-reverse pl-10" : "pr-10")}
    >
      <OrgAvatar name={entry.org.name} seed={entry.org.slug} size={24} className="mt-0.5" />
      <div className={cx("flex min-w-0 flex-col gap-1", entry.ours ? "items-end" : "items-start")}>
        <header className="flex flex-wrap items-baseline gap-x-1.5 text-[12.5px]">
          <span className="font-medium text-ink">{author}</span>
          <Tooltip content={dateTime(entry.at, calendar)} side="top">
            <time dateTime={entry.at} className="text-muted tabular-nums">
              {clockTime(entry.at)}
            </time>
          </Tooltip>
        </header>
        <div
          className={cx(
            "max-w-full rounded-[18px] px-4 py-2.5 text-[14px] leading-[1.6] break-words whitespace-pre-wrap",
            entry.ours ? "rounded-tr-md bg-bubble text-ink" : "rounded-tl-md border border-border bg-surface text-ink shadow-card",
          )}
        >
          {entry.body}
        </div>
        {provenance !== null && <p className="px-1 text-[12px] text-muted">{provenance}</p>}
      </div>
    </article>
  );
}

/** A representative closed the thread: a divider both sides see. Its summary stays with its own org. */
function CloseRow({ entry }: { entry: CloseEntry }) {
  return (
    <div role="separator" aria-label={`${representativeName(entry.org.name)} closed the thread`} className="flex items-center gap-3">
      <span className="h-px flex-1 bg-border" aria-hidden />
      <span className="flex items-center gap-1.5 text-[12.5px] text-muted">
        <OrgAvatar name={entry.org.name} seed={entry.org.slug} size={16} />
        {representativeName(entry.org.name)} closed the thread · {clockTime(entry.at)}
      </span>
      <span className="h-px flex-1 bg-border" aria-hidden />
    </div>
  );
}

// --- Our side's rows -----------------------------------------------------------------------

/** Linear's activity line: a small icon, the sentence, the age; expandable to its detail. */
function SideRow({
  icon,
  children,
  at,
  detail,
  owner,
}: {
  icon: ReactNode;
  children: ReactNode;
  at: string;
  detail?: ReactNode;
  owner: OrgRef;
}) {
  const [open, setOpen] = useState(false);
  const expandable = detail !== undefined;
  const line = (
    <>
      <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-hover text-secondary">{icon}</span>
      <span className="min-w-0 flex-1 text-[13px] leading-5 text-secondary">
        {children}
        <span className="text-muted"> · {clockTime(at)}</span>
      </span>
      <Tooltip content={`Only ${owner.name} can see this`} side="left">
        <span className="shrink-0 text-faint" aria-label={`Only ${owner.name} can see this`}>
          <Lock size={12} strokeWidth={1.75} />
        </span>
      </Tooltip>
      {expandable && (
        <ChevronRight
          size={14}
          strokeWidth={1.5}
          aria-hidden
          className={cx("shrink-0 text-muted transition-transform", open && "rotate-90")}
        />
      )}
    </>
  );
  return (
    <div className="flex flex-col gap-2">
      {expandable ? (
        <button
          type="button"
          aria-expanded={open}
          onClick={() => {
            setOpen((value) => !value);
          }}
          className="-mx-2 flex items-center gap-2.5 rounded-control px-2 py-1 text-left hover:bg-hover/60"
        >
          {line}
        </button>
      ) : (
        <div className="-mx-2 flex items-center gap-2.5 px-2 py-1">{line}</div>
      )}
      {open && detail}
    </div>
  );
}

function Quote({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="ml-[30px] rounded-[12px] border border-border bg-surface px-3.5 py-2.5">
      <p className="text-[11.5px] font-medium tracking-wide text-muted uppercase">{label}</p>
      <p className="mt-1 font-serif text-[15px] leading-[1.6] break-words whitespace-pre-wrap text-ink">{children}</p>
    </div>
  );
}

function ConsultRow({ entry, us }: { entry: ConsultEntry; us: OrgRef }) {
  const { consult } = entry;
  const preview = consult.question.length > 90 ? `${consult.question.slice(0, 88).trimEnd()}…` : consult.question;
  return (
    <SideRow
      icon={<MessageCircleQuestion size={12} strokeWidth={1.75} />}
      at={entry.at}
      owner={us}
      detail={<Quote label="Question for the team">{consult.question}</Quote>}
    >
      <span className="font-medium text-ink">{representativeName(us.name)}</span> asked the team: “{preview}”
    </SideRow>
  );
}

function ReleaseRow({ entry, us }: { entry: ReleaseEntry; us: OrgRef }) {
  const who = entry.by === null ? "Claude" : firstName(entry.by.name);
  const how = entry.by === null ? "answered without a teammate" : "released the answer";
  const text = entry.consult.released_text;
  return (
    <SideRow
      icon={<ArrowUpFromLine size={12} strokeWidth={1.75} />}
      at={entry.at}
      owner={us}
      detail={text === null ? undefined : <Quote label={`Released to ${representativeName(us.name)}`}>{text}</Quote>}
    >
      <span className="font-medium text-ink">{who}</span> {how} <span className="text-muted">(this thread only)</span>
    </SideRow>
  );
}

function resolutionSentence(entry: ResolutionEntry, them: OrgRef): string {
  const { kind, resolution } = entry.escalation;
  switch (resolution?.action) {
    case "approve":
      return kind === "consult" ? "approved the answer" : "approved Claude's reply";
    case "decline":
      return kind === "consult" ? "declined to answer" : "declined Claude's reply";
    case "take_over":
      return `replied to ${them.name} directly`;
    case undefined:
      return "resolved the item";
  }
}

const RESOLUTION_ICON: Record<EscalationAction, ReactNode> = {
  approve: <Check size={12} strokeWidth={2} />,
  decline: <X size={12} strokeWidth={2} />,
  take_over: <UserRound size={12} strokeWidth={1.75} />,
};

function ResolutionRow({ entry, us, them }: { entry: ResolutionEntry; us: OrgRef; them: OrgRef }) {
  const resolution = entry.escalation.resolution;
  const text = resolution?.text ?? null;
  const who = entry.by === null ? "A teammate" : firstName(entry.by.name);
  return (
    <SideRow
      icon={resolution === null ? <Check size={12} strokeWidth={2} /> : RESOLUTION_ICON[resolution.action]}
      at={entry.at}
      owner={us}
      detail={text === null || text === "" ? undefined : <Quote label="What they said">{text}</Quote>}
    >
      <span className="font-medium text-ink">{who}</span> {resolutionSentence(entry, them)}
    </SideRow>
  );
}

// --- Internal notes ------------------------------------------------------------------------

/** Highlights `@slug` mentions in a note body. */
function withMentions(body: string): ReactNode[] {
  return body.split(/(@[a-z0-9][a-z0-9_-]*)/gi).map((part, index) =>
    part.startsWith("@") ? (
      <span key={index} className="rounded-[4px] bg-accent-soft px-0.5 font-medium text-accent-strong">
        {part}
      </span>
    ) : (
      part
    ),
  );
}

function NoteRow({ entry, us }: { entry: NoteEntry; us: OrgRef }) {
  const { note } = entry;
  const name = note.author_kind === "claude" ? "Claude" : (note.author?.name ?? "A teammate");
  return (
    <article aria-label={`Internal note from ${name}`} className="flex gap-2.5">
      {note.author_kind === "claude" ? (
        <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full bg-bubble">
          <Spark size={14} />
        </span>
      ) : (
        <Avatar name={name} seed={us.slug} size={24} className="mt-0.5" />
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <header className="flex items-baseline gap-1.5 text-[12.5px]">
          <span className="font-medium text-ink">{name}</span>
          <Tooltip content={`Only ${us.name} can see notes`} side="top">
            <span className="flex items-center gap-1 text-muted">
              <Lock size={11} strokeWidth={1.75} aria-hidden /> Internal note
            </span>
          </Tooltip>
          <span className="text-muted">· {clockTime(note.created_at)}</span>
        </header>
        <div className="rounded-[14px] border border-dashed border-border-strong bg-sidebar px-3.5 py-2.5 text-[13.5px] leading-[1.6] break-words whitespace-pre-wrap text-ink">
          {withMentions(note.body)}
        </div>
      </div>
    </article>
  );
}

// --- Typing --------------------------------------------------------------------------------

function TypingRow({ typing, us, them }: { typing: Typing; us: OrgRef; them: OrgRef }) {
  const ours = typing.orgId === us.id;
  const org = ours ? us : them;
  return (
    <div className={cx("flex items-center gap-2.5", ours && "flex-row-reverse")} role="status" aria-live="polite">
      <Spark size={20} animated />
      {ours && typing.text !== "" ? (
        <div className="max-w-[80%] rounded-[18px] rounded-tr-md bg-bubble px-4 py-2.5 text-[14px] leading-[1.6] break-words whitespace-pre-wrap text-ink">
          {typing.text}
        </div>
      ) : (
        <span className="text-[13px] text-muted">{representativeName(org.name)} is writing…</span>
      )}
    </div>
  );
}
