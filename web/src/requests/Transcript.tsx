/**
 * The conversation between the two representatives (LIVE_DEMO_PLAN 3.9), read like a
 * chat: every message left-aligned with its author's avatar, name and time over a serif
 * body, in full, with no bubbles, since both sides are agents. Our side's comments,
 * edits and decisions sit inline as rows only our org sees; each run that ended has its
 * Closed row; the last row says whose move it is, or shows our Claude writing.
 */
import type { ReactNode } from "react";

import { useCalendar } from "../demo/worldClock";
import { firstName } from "../shell/names";
import { Avatar, OrgAvatar, Spark, Tooltip } from "../shell/ui";
import { ReleaseRow, ResolutionRow, SteerRow, WaitingLine, type RowActions } from "./EventRows";
import { EndedRow, ReopenRow } from "./RunRows";
import { clockTime, dateTime, representativeName } from "./format";
import type { Typing } from "./live";
import { mentionParts } from "./mentions";
import type { CloseEntry, MessageEntry, NoteEntry, ReopenEntry, TimelineEntry } from "./timeline";
import type { NoteView, OrgRef } from "./types";

const BODY = "font-serif text-ui-md break-words whitespace-pre-wrap text-ink";

interface TranscriptProps {
  entries: readonly TimelineEntry[];
  us: OrgRef;
  them: OrgRef;
  /** Our Claude writing to them now. The other side's never shows. */
  typing: Typing | null;
  /** The steady last row: whose move it is (`waitingLine`). */
  waiting: string | null;
  actions: RowActions;
}

/**
 * Our attempts to reopen since the latest run ended that haven't opened it: while there
 * are any, the Closed row has no button, and the last one carries the next step.
 */
function pendingAttempts(entries: readonly TimelineEntry[]): ReopenEntry[] {
  let latest = -1;
  entries.forEach((entry, index) => {
    if (entry.type === "ended" && entry.latest) latest = index;
  });
  if (latest < 0) return [];
  return entries
    .slice(latest + 1)
    .filter((entry): entry is ReopenEntry => entry.type === "reopen" && entry.ours && entry.outcome !== "opened");
}

export function Transcript({ entries, us, them, typing, waiting, actions }: TranscriptProps) {
  const attempts = pendingAttempts(entries);
  const lastAttempt = attempts.at(-1)?.id ?? null;
  const row = (entry: TimelineEntry): ReactNode => {
    switch (entry.type) {
      case "message":
        return <MessageRow entry={entry} />;
      case "close":
        return <CloseRow entry={entry} />;
      case "release":
        return <ReleaseRow entry={entry} us={us} />;
      case "resolution":
        return <ResolutionRow entry={entry} us={us} them={them} />;
      case "note":
        return <CommentRow entry={entry} />;
      case "steer":
        return <SteerRow entry={entry} us={us} actions={actions} />;
      case "ended":
        return <EndedRow entry={entry} us={us} them={them} actions={actions} attempted={attempts.length > 0} />;
      case "reopen":
        return <ReopenRow entry={entry} us={us} them={them} actions={actions} retryable={entry.id === lastAttempt} />;
    }
  };
  return (
    <ol aria-label="Conversation" className="flex flex-col gap-6">
      {entries.map((entry) => (
        <li key={entry.id}>{row(entry)}</li>
      ))}
      {typing !== null && (
        <li>
          <TypingRow typing={typing} us={us} />
        </li>
      )}
      {waiting !== null && (
        <li>
          <WaitingLine text={waiting} />
        </li>
      )}
    </ol>
  );
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
        <header className="flex flex-wrap items-baseline gap-x-1.5 text-ui-sm text-muted">
          <span className="font-medium text-secondary">{author}</span>
          {at !== null && (
            <Tooltip content={dateTime(at, calendar)} side="top">
              <time dateTime={at} className="text-ui-xs tabular-nums">
                {clockTime(at)}
              </time>
            </Tooltip>
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
      <p className={BODY}>{entry.body}</p>
      {provenance !== null && <p className="text-ui-xs text-muted">{provenance}</p>}
    </Authored>
  );
}

/** A representative closed the conversation: a divider both sides see. Its summary stays with its own org. */
function CloseRow({ entry }: { entry: CloseEntry }) {
  const who = representativeName(entry.org.name);
  return (
    <div role="separator" aria-label={`${who} closed the conversation`} className="flex items-center gap-3">
      <span className="h-px flex-1 bg-border" aria-hidden />
      <span className="flex items-center gap-1.5 text-ui-xs text-muted">
        <OrgAvatar name={entry.org.name} seed={entry.org.slug} size={16} />
        {who} closed the conversation · {clockTime(entry.at)}
      </span>
      <span className="h-px flex-1 bg-border" aria-hidden />
    </div>
  );
}

// --- Comments ------------------------------------------------------------------------------

/** Highlights the comment's stored mentions: an `@` that shared nothing reads as plain text. */
function withMentions(note: NoteView): ReactNode[] {
  const slugs = new Set(note.mentions.map((member) => member.slug));
  return mentionParts(note.body, slugs).map((part, index) =>
    part.mention ? (
      <span key={index} className="font-medium text-link">
        {part.text}
      </span>
    ) : (
      part.text
    ),
  );
}

/**
 * A comment for our team, in amber so it never reads as a message to the other side.
 * No Claude reads it. Claude's own note (it couldn't write a summary) reads the same.
 */
function CommentRow({ entry }: { entry: NoteEntry }) {
  const { note } = entry;
  const name = note.author_kind === "claude" ? "Claude" : (note.author?.name ?? "A teammate");
  return (
    <article
      aria-label={`Comment from ${name}`}
      className="rounded-r-[8px] border-l-2 border-caution/45 bg-caution-soft/55 px-3 py-2"
    >
      <p className="text-ui-xs text-muted">
        <span className="font-medium text-secondary">{name}</span> · comment · {clockTime(note.created_at)}
      </p>
      <p className="mt-0.5 text-ui break-words whitespace-pre-wrap text-ink">{withMentions(note)}</p>
    </article>
  );
}

// --- Typing --------------------------------------------------------------------------------

/**
 * Our representative writing now, streaming its text in the message style. Only a fixed
 * line is announced: the streamed text grows with every delta, and a live region would
 * read all of it again each time.
 */
function TypingRow({ typing, us }: { typing: Typing; us: OrgRef }) {
  const author = representativeName(us.name);
  return (
    <>
      <p role="status" className="sr-only">
        {author} is writing…
      </p>
      <Authored
        label={`${author} is writing`}
        author={author}
        at={null}
        avatar={<OrgAvatar name={us.name} seed={us.slug} size={28} />}
      >
        {typing.text !== "" ? (
          <p className={BODY}>{typing.text}</p>
        ) : (
          <p aria-hidden className="flex items-center gap-2 text-ui text-muted">
            <Spark size={16} animated />
            {author} is writing…
          </p>
        )}
      </Authored>
    </>
  );
}
