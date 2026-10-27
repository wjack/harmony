/**
 * The thread between the two representatives (LIVE_DEMO_PLAN 3.9), read like a chat:
 * every message left-aligned with its author's avatar, name and time over a serif body,
 * no bubbles, since both sides are agents. Our side's consults, releases, resolutions
 * and internal notes sit inline as rows only our org sees.
 */
import { ArrowUpFromLine, Check, ChevronRight, Lock, Undo2, UserRound, X } from "lucide-react";
import { useState, type ReactNode } from "react";

import { useCalendar } from "../demo/worldClock";
import { firstName } from "../shell/names";
import { Avatar, ClampedText, OrgAvatar, Spark, Tooltip, cx } from "../shell/ui";
import { clockTime, dateTime, representativeName } from "./format";
import type { Typing } from "./live";
import { mentionParts } from "./mentions";
import type {
  CloseEntry,
  ConsultEntry,
  MessageEntry,
  NoteEntry,
  ReleaseEntry,
  ResolutionEntry,
  TimelineEntry,
} from "./timeline";
import type { EscalationAction, NoteView, OrgRef } from "./types";

/** Past this many lines a message is clamped, with "View more" (3.11). */
const MESSAGE_LINES = 12;

const BODY = "font-serif text-[16px] leading-[1.6] break-words whitespace-pre-wrap text-ink";

interface TranscriptProps {
  entries: readonly TimelineEntry[];
  us: OrgRef;
  them: OrgRef;
  typing: Typing | null;
}

export function Transcript({ entries, us, them, typing }: TranscriptProps) {
  return (
    <ol aria-label="Thread" className="flex flex-col gap-6">
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

/** A row in the message style: the avatar, then "author · time" over the body. */
function Authored({
  avatar,
  author,
  at,
  label,
  children,
}: {
  avatar: ReactNode;
  author: string;
  at: string | null;
  label: string;
  children: ReactNode;
}) {
  const calendar = useCalendar();
  return (
    <article aria-label={label} className="flex gap-3">
      <span className="mt-0.5 shrink-0">{avatar}</span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <header className="flex flex-wrap items-baseline gap-x-1.5 text-[13px] leading-5 text-muted">
          <span className="font-medium text-secondary">{author}</span>
          {at !== null && (
            <>
              <span aria-hidden>·</span>
              <Tooltip content={dateTime(at, calendar)} side="top">
                <time dateTime={at} className="tabular-nums">
                  {clockTime(at)}
                </time>
              </Tooltip>
            </>
          )}
        </header>
        {children}
      </div>
    </article>
  );
}

function MessageRow({ entry }: { entry: MessageEntry }) {
  const provenance = provenanceLabel(entry);
  const person = entry.provenance === "human" && entry.approvedBy !== null ? entry.approvedBy : null;
  const author = person?.name ?? representativeName(entry.org.name);
  return (
    <Authored
      label={`${author}, ${clockTime(entry.at)}`}
      author={author}
      at={entry.at}
      avatar={
        person === null ? (
          <OrgAvatar name={entry.org.name} seed={entry.org.slug} size={28} />
        ) : (
          <Avatar name={person.name} seed={entry.org.slug} size={28} />
        )
      }
    >
      <ClampedText lines={MESSAGE_LINES} className={BODY}>
        {entry.body}
      </ClampedText>
      {provenance !== null && <p className="text-[12.5px] text-muted">{provenance}</p>}
    </Authored>
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

/**
 * One line of our side's activity: an icon, the sentence and the time; expandable to its
 * detail. `owner` puts the lock that says only our org sees it at the end, for a
 * sentence that doesn't say so itself.
 */
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
  owner?: OrgRef;
}) {
  const [open, setOpen] = useState(false);
  const expandable = detail !== undefined;
  const line = (
    <>
      <span className="flex size-7 shrink-0 items-center justify-center text-muted">{icon}</span>
      <span className="min-w-0 flex-1 text-[13.5px] leading-5 text-secondary">
        {children}
        <span className="text-muted"> · {clockTime(at)}</span>
      </span>
      {owner !== undefined && (
        <Tooltip content={`Only ${owner.name} can see this`} side="left">
          <span className="shrink-0 text-faint" aria-label={`Only ${owner.name} can see this`}>
            <Lock size={12} strokeWidth={1.75} />
          </span>
        </Tooltip>
      )}
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
          className="-mx-2 flex items-center gap-3 rounded-control px-2 py-0.5 text-left hover:bg-hover/60"
        >
          {line}
        </button>
      ) : (
        <div className="-mx-2 flex items-center gap-3 px-2 py-0.5">{line}</div>
      )}
      {open && detail}
    </div>
  );
}

function Quote({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="ml-10 rounded-[12px] border border-border bg-surface px-3.5 py-2.5">
      <p className="text-[11.5px] font-medium tracking-wide text-muted uppercase">{label}</p>
      <p className="mt-1 font-serif text-[15px] leading-[1.6] break-words whitespace-pre-wrap text-ink">{children}</p>
    </div>
  );
}

/**
 * Our representative asked the team. One line, no preview: the decision card already
 * shows the short question, and the whole question is behind the chevron.
 */
function ConsultRow({ entry, us }: { entry: ConsultEntry; us: OrgRef }) {
  return (
    <SideRow
      icon={<Lock size={14} strokeWidth={1.75} aria-hidden />}
      at={entry.at}
      detail={<Quote label="Question for the team">{entry.consult.question}</Quote>}
    >
      Our Claude asked the team · only {us.name} sees this
    </SideRow>
  );
}

function ReleaseRow({ entry, us }: { entry: ReleaseEntry; us: OrgRef }) {
  const who = entry.by === null ? "Claude" : firstName(entry.by.name);
  const how = entry.by === null ? "answered without a teammate" : "released the answer";
  const text = entry.consult.released_text;
  return (
    <SideRow
      icon={<ArrowUpFromLine size={14} strokeWidth={1.75} />}
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
    case "revise":
      return "sent Claude's reply back to be revised";
    case undefined:
      return "resolved the item";
  }
}

const RESOLUTION_ICON: Record<EscalationAction, ReactNode> = {
  approve: <Check size={14} strokeWidth={2} />,
  decline: <X size={14} strokeWidth={2} />,
  take_over: <UserRound size={14} strokeWidth={1.75} />,
  revise: <Undo2 size={14} strokeWidth={1.75} />,
};

function ResolutionRow({ entry, us, them }: { entry: ResolutionEntry; us: OrgRef; them: OrgRef }) {
  const resolution = entry.escalation.resolution;
  const text = resolution?.text ?? null;
  const who = entry.by === null ? "A teammate" : firstName(entry.by.name);
  return (
    <SideRow
      icon={resolution === null ? <Check size={14} strokeWidth={2} /> : RESOLUTION_ICON[resolution.action]}
      at={entry.at}
      owner={us}
      detail={text === null || text === "" ? undefined : <Quote label="What they said">{text}</Quote>}
    >
      <span className="font-medium text-ink">{who}</span> {resolutionSentence(entry, them)}
    </SideRow>
  );
}

// --- Internal notes ------------------------------------------------------------------------

/** Highlights the note's stored mentions: an `@` that shared nothing reads as plain text. */
function withMentions(note: NoteView): ReactNode[] {
  const slugs = new Set(note.mentions.map((member) => member.slug));
  return mentionParts(note.body, slugs).map((part, index) =>
    part.mention ? (
      <span key={index} className="rounded-[4px] bg-accent-soft px-0.5 font-medium text-accent-strong">
        {part.text}
      </span>
    ) : (
      part.text
    ),
  );
}

function NoteRow({ entry, us }: { entry: NoteEntry; us: OrgRef }) {
  const { note } = entry;
  const name = note.author_kind === "claude" ? "Claude" : (note.author?.name ?? "A teammate");
  return (
    <article aria-label={`Internal note from ${name}`} className="flex gap-3">
      {note.author_kind === "claude" ? (
        <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center">
          <Spark size={16} />
        </span>
      ) : (
        <Avatar name={name} seed={us.slug} size={28} className="mt-0.5" />
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <header className="flex flex-wrap items-baseline gap-x-1.5 text-[13px] leading-5 text-muted">
          <span className="font-medium text-secondary">{name}</span>
          <Tooltip content={`Only ${us.name} can see notes`} side="top">
            <span className="flex items-center gap-1">
              <Lock size={11} strokeWidth={1.75} aria-hidden /> Internal note
            </span>
          </Tooltip>
          <span>· {clockTime(note.created_at)}</span>
        </header>
        <div className="rounded-[12px] border border-dashed border-border-strong bg-sidebar px-3.5 py-2.5 text-[14px] leading-[1.6] break-words whitespace-pre-wrap text-ink">
          {withMentions(note)}
        </div>
      </div>
    </article>
  );
}

// --- Typing --------------------------------------------------------------------------------

/**
 * A representative writing now: ours streams its text in the message style; the
 * counterparty's only shows that it writes. Only a fixed line is announced: the streamed
 * text grows with every delta, and a live region would read all of it again each time.
 */
function TypingRow({ typing, us, them }: { typing: Typing; us: OrgRef; them: OrgRef }) {
  const ours = typing.orgId === us.id;
  const org = ours ? us : them;
  const author = representativeName(org.name);
  return (
    <>
      <p role="status" className="sr-only">
        {author} is writing…
      </p>
      <Authored
        label={`${author} is writing`}
        author={author}
        at={null}
        avatar={<OrgAvatar name={org.name} seed={org.slug} size={28} />}
      >
        {ours && typing.text !== "" ? (
          <p className={BODY}>{typing.text}</p>
        ) : (
          <p aria-hidden className="flex items-center gap-2 text-[14px] text-muted">
            <Spark size={16} animated />
            {author} is writing…
          </p>
        )}
      </Authored>
    </>
  );
}
