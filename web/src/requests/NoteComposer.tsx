/**
 * Internal notes (ARCHITECTURE §10): org-only discussion on a thread, never seen by
 * the representative or the counterparty. `@slug` mentions share the thread with that
 * teammate; typing `@` suggests the org's members.
 */
import { ArrowUp, Lock } from "lucide-react";
import { useRef, useState, type KeyboardEvent } from "react";

import { Avatar, Tooltip, cx } from "../shell/ui";
import { problemOf, useAddNote } from "./api";
import type { MemberView, OrgRef } from "./types";

/** The `@partial` being typed at the caret, if any. */
function mentionAt(text: string, caret: number): { start: number; query: string } | null {
  const match = /(^|\s)@([a-z0-9_-]*)$/i.exec(text.slice(0, caret));
  if (match === null) return null;
  const query = match[2] ?? "";
  return { start: caret - query.length - 1, query: query.toLowerCase() };
}

interface NoteComposerProps {
  threadId: string;
  us: OrgRef;
  members: readonly MemberView[];
  memberId: string | undefined;
}

export function NoteComposer({ threadId, us, members, memberId }: NoteComposerProps) {
  const addNote = useAddNote(threadId);
  const [body, setBody] = useState("");
  const [caret, setCaret] = useState(0);
  const [highlight, setHighlight] = useState(0);
  const [focused, setFocused] = useState(false);
  const field = useRef<HTMLTextAreaElement>(null);
  // One line until it's in use, like Linear's comment box.
  const expanded = focused || body !== "";

  const mention = mentionAt(body, caret);
  const suggestions =
    mention === null
      ? []
      : members
          .filter((member) => member.id !== memberId)
          .filter(
            (member) =>
              member.slug.toLowerCase().startsWith(mention.query) ||
              member.name.toLowerCase().split(/\s+/).some((part) => part.startsWith(mention.query)),
          )
          .slice(0, 5);

  const insert = (member: MemberView) => {
    if (mention === null) return;
    const next = `${body.slice(0, mention.start)}@${member.slug} ${body.slice(caret)}`;
    const position = mention.start + member.slug.length + 2;
    setBody(next);
    setCaret(position);
    setHighlight(0);
    requestAnimationFrame(() => {
      field.current?.setSelectionRange(position, position);
      field.current?.focus();
    });
  };

  const submit = () => {
    const trimmed = body.trim();
    if (trimmed === "" || addNote.isPending) return;
    addNote.mutate(
      { body: trimmed },
      {
        onSuccess: () => {
          setBody("");
          setCaret(0);
        },
      },
    );
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (suggestions.length > 0) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        setHighlight((index) => (index + step + suggestions.length) % suggestions.length);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        const member = suggestions[Math.min(highlight, suggestions.length - 1)];
        if (member !== undefined) insert(member);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  const error = addNote.error === null ? null : (problemOf(addNote.error)?.detail ?? addNote.error.message);
  const ready = body.trim() !== "" && !addNote.isPending;

  return (
    <div className="relative">
      {suggestions.length > 0 && (
        <ul
          role="listbox"
          aria-label="Mention a teammate"
          className="absolute bottom-full left-3 z-10 mb-1.5 w-64 rounded-menu border border-border bg-surface p-1.5 shadow-menu"
        >
          {suggestions.map((member, index) => (
            <li key={member.id} role="option" aria-selected={index === highlight}>
              <button
                type="button"
                onMouseDown={(event) => {
                  event.preventDefault();
                  insert(member);
                }}
                className={cx(
                  "flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-[13px]",
                  index === highlight && "bg-hover",
                )}
              >
                <Avatar name={member.name} seed={us.slug} size={20} />
                <span className="truncate">{member.name}</span>
                <span className="ml-auto text-[12px] text-muted">@{member.slug}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="rounded-[16px] border border-border bg-surface shadow-card focus-within:border-border-strong">
        <div className="flex items-start">
          <textarea
            ref={field}
            aria-label="Internal note"
            value={body}
            rows={expanded ? 2 : 1}
            placeholder="Add an internal note… Use @ to bring in a teammate"
            onChange={(event) => {
              setBody(event.target.value);
              setCaret(event.target.selectionStart);
            }}
            onSelect={(event) => {
              setCaret(event.currentTarget.selectionStart);
            }}
            onFocus={() => {
              setFocused(true);
            }}
            onBlur={() => {
              setFocused(false);
            }}
            onKeyDown={onKeyDown}
            className={cx(
              "block min-w-0 flex-1 resize-none bg-transparent px-4 pt-3 text-[14px] leading-[1.55] text-ink outline-none placeholder:text-muted",
              !expanded && "pb-3",
            )}
          />
          {!expanded && (
            <Tooltip content={`Only ${us.name} can see notes`} side="top">
              <span className="mt-3.5 mr-4 shrink-0 text-faint" aria-label={`Only ${us.name} can see notes`}>
                <Lock size={13} strokeWidth={1.75} />
              </span>
            </Tooltip>
          )}
        </div>
        {expanded && (
          <div className="flex items-center gap-2 px-3 pt-1 pb-2.5">
            <span className="flex items-center gap-1 pl-1 text-[12px] text-muted">
              <Lock size={11} strokeWidth={1.75} aria-hidden /> Only {us.name} can see notes
            </span>
            <span className="flex-1" />
            {error !== null && (
              <span role="alert" className="truncate text-[12px] text-danger">
                {error}
              </span>
            )}
            <button
              type="button"
              aria-label="Add note"
              disabled={!ready}
              onClick={submit}
              className={cx(
                "inline-flex size-8 items-center justify-center rounded-control text-white transition-colors",
                ready ? "bg-accent-strong hover:brightness-95" : "bg-accent-strong/40",
              )}
            >
              <ArrowUp size={16} strokeWidth={2} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
