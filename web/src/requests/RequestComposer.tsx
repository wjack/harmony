/**
 * The request view's box (EDIT.md §8.3, composer.html), in one of two modes switched in
 * its bottom bar. **Comment** writes to our team: no Claude reads it, and an @mention
 * brings a teammate in. **Edit** tells our own Claude what to do differently: it
 * rewrites a draft of ours that's waiting and follows the instruction for the rest of
 * the conversation. The box starts in Comment and goes back to it after each Edit.
 *
 * Enter sends what's in the box and never approves: approving belongs to the card.
 * An Edit's `client_key` is minted per submit and reused only after a request that got
 * no answer, so the retry is the same write; any answer, even an error, ends it. Every
 * failure keeps the typed text.
 *
 * Edit is off on a closed conversation (Reopen is how Claude picks it up), and Tell
 * Claude is off while the budget stops live turns; Comment always works.
 */
import { CircleAlert, Loader2, Lock, MessageSquare, Sparkles } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import type { SteerView } from "../api/types";
import { budgetLine, useBudgetState } from "../chat/failureCopy";
import { firstName, shortOrgName } from "../shell/names";
import { Avatar, Button, Tooltip, cx } from "../shell/ui";
import { ClaudeButton } from "./ClaudeButton";
import { problemOf, useAddNote, useSteer } from "./api";
import { useAutoHeight } from "./autoHeight";
import { editError, type EditError } from "./failure";
import { possessive } from "./format";
import { mentionAt, mentionedSlugs, mentionParts } from "./mentions";
import type { MemberView, OrgRef } from "./types";
import type { Composer, ComposerMode } from "./useComposer";
import type { Decision } from "./useDecision";

const MAX_FIELD_PX = 240;

// The field's text box, shared by the textarea and the backdrop that colours its mentions:
// the two must wrap and scroll alike, or the caret drifts off the text it edits.
const FIELD_BOX = "scrollbar-quiet block min-h-12 w-full px-4 pt-3 pb-1 text-ui-md break-words whitespace-pre-wrap";

const PLACEHOLDER: Record<ComposerMode, string> = {
  comment: "Comment for your team… Use @ to bring someone in",
  edit: "What should Claude do differently?",
};

interface RequestComposerProps {
  threadId: string;
  us: OrgRef;
  /** Our org's members, for @mentions. */
  members: readonly MemberView[];
  memberId: string | undefined;
  composer: Composer;
  /** The open item, whose draft an Edit rewrites. */
  decision: Decision | null;
  /** The conversation has ended: Edit is off until it's reopened. */
  closed: boolean;
  /** An Edit of our side's that Claude is still working on. */
  running: SteerView | null;
}

/** A submit that got no answer, kept so the retry of the same text is the same write. */
interface Unanswered {
  key: string;
  body: string;
}

export function RequestComposer({ threadId, us, members, memberId, composer, decision, closed, running }: RequestComposerProps) {
  const addNote = useAddNote(threadId);
  const steer = useSteer(threadId);
  const budget = useBudgetState();
  const { text } = composer;
  const mode: ComposerMode = closed ? "comment" : composer.mode;
  const editing = mode === "edit";
  const [caret, setCaret] = useState(0);
  const [highlight, setHighlight] = useState(0);
  const [unanswered, setUnanswered] = useState<Unanswered | null>(null);
  const [failure, setFailure] = useState<EditError | null>(null);
  const field = useAutoHeight(text, MAX_FIELD_PX, PLACEHOLDER[mode]);
  const busy = addNote.isPending || steer.isPending;
  const typed = text.trim() !== "";
  const spent = budgetLine(budget);

  // Something above asked for the field (Try again, a reopen moved here): give it focus.
  useEffect(() => {
    if (composer.focusRequest === 0) return;
    const element = field.current;
    element?.focus();
    element?.setSelectionRange(element.value.length, element.value.length);
  }, [composer.focusRequest, field]);

  /**
   * Where the caret goes once an inserted mention is on screen. Placed as the text
   * renders, before the next keystroke: a frame later, text typed meanwhile would land
   * after it.
   */
  const caretAfterInsert = useRef<number | null>(null);
  useLayoutEffect(() => {
    const at = caretAfterInsert.current;
    if (at === null) return;
    caretAfterInsert.current = null;
    field.current?.setSelectionRange(at, at);
  }, [text, field]);

  // Esc closes the mention list where it was opened; typing or moving the caret reopens it.
  const [dismissedAt, setDismissedAt] = useState<string | null>(null);
  const place = `${String(caret)}:${text}`;
  const partial = mentionAt(text, caret);
  const mention = editing || dismissedAt === place ? null : partial;
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
  const active = Math.min(highlight, suggestions.length - 1);
  // The field keeps its textbox role (a textarea allows no other), so the list is tied to
  // it by `aria-controls` and `aria-activedescendant`, and a status says it opened.
  const listId = useId();
  const optionId = (index: number) => `${listId}-${String(index)}`;
  // Edit has no mentions: an @ there is likely meant for the team.
  const mentionInEdit = editing && (partial !== null || mentionedSlugs(text).length > 0);
  // A textarea can't colour part of its text, so while a comment mentions a teammate its
  // own glyphs go transparent over a copy that colours the mention (same weight, or the
  // copy would no longer sit under the caret).
  const parts = editing ? [] : mentionParts(text, new Set(members.map((member) => member.slug)));
  const highlighted = parts.some((part) => part.mention);
  const backdrop = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (backdrop.current !== null && field.current !== null) backdrop.current.scrollTop = field.current.scrollTop;
  });

  const write = (value: string, at = value.length) => {
    composer.setText(value);
    setCaret(at);
  };

  const switchTo = (next: ComposerMode) => {
    composer.setMode(next);
    setFailure(null);
    field.current?.focus();
  };

  const insert = (member: MemberView) => {
    if (mention === null) return;
    const rest = text.slice(caret);
    // One space after the mention, and the caret after it.
    const next = `${text.slice(0, mention.start)}@${member.slug}${/^\s/.test(rest) ? "" : " "}${rest}`;
    const position = mention.start + member.slug.length + 2;
    caretAfterInsert.current = position;
    write(next, position);
    setHighlight(0);
    field.current?.focus();
  };

  const comment = () => {
    const body = text.trim();
    if (body === "" || busy) return;
    addNote.mutate(
      { body },
      {
        onSuccess: () => {
          write("");
        },
      },
    );
  };

  const tellClaude = () => {
    const body = text.trim();
    if (body === "" || busy || spent !== null) return;
    const key = unanswered?.body === body ? unanswered.key : crypto.randomUUID();
    const item = decision?.escalation.id ?? null;
    setFailure(null);
    steer.mutate(
      {
        client_key: key,
        body,
        escalation_id: item,
        // The rewrite starts from the member's wording of the draft, not Claude's.
        draft_text: decision?.edited === true ? decision.answer : null,
      },
      {
        onSuccess: (started) => {
          setUnanswered(null);
          if (started.draft_rev !== null && started.escalation_id === item) {
            decision?.awaitRewrite(started.draft_rev, started.steer_id);
          }
          write("");
          composer.setMode("comment");
        },
        onError: (error) => {
          const shown = editError(error, problemOf(error), memberId);
          setUnanswered(shown.sameKey ? { key, body } : null);
          setFailure(shown);
        },
      },
    );
  };

  const send = editing ? tellClaude : comment;

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
        const member = suggestions[active];
        if (member !== undefined) insert(member);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setDismissedAt(place);
        return;
      }
    }
    // Enter sends the box's text; it never approves, even with ⌘ and nothing typed.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (typed) send();
    }
  };

  const noteError = addNote.error === null ? null : (problemOf(addNote.error)?.detail ?? addNote.error.message);
  const runningBy = running === null ? null : running.author.id === memberId ? "your" : possessive(firstName(running.author.name));

  let caption: ReactNode;
  if (failure !== null) {
    caption = (
      <Caption tone="danger" icon={<CircleAlert size={14} strokeWidth={1.75} />} role="alert">
        {failure.text}
        {failure.retry && (
          <>
            {" "}
            <InlineLink onClick={tellClaude}>Try again</InlineLink>
          </>
        )}
      </Caption>
    );
  } else if (noteError !== null && !editing) {
    caption = (
      <Caption tone="danger" icon={<CircleAlert size={14} strokeWidth={1.75} />} role="alert">
        {noteError}
      </Caption>
    );
  } else if (!editing) {
    caption = (
      <Caption tone="muted" icon={<Lock size={13} strokeWidth={1.75} />}>
        Only {shortOrgName(us.name)} sees comments. Claude doesn't read them.
      </Caption>
    );
  } else if (spent !== null) {
    caption = (
      <Caption tone="danger" icon={<CircleAlert size={14} strokeWidth={1.75} />}>
        {spent}. Tell Claude works again once it's back.
      </Caption>
    );
  } else if (runningBy !== null) {
    caption = (
      <Caption tone="info" icon={<Loader2 size={13} strokeWidth={2} className="animate-spin" />}>
        Claude is still working on {runningBy} edit.
      </Caption>
    );
  } else {
    const waitingDraft = decision !== null;
    caption = (
      <Caption tone="info" icon={<Sparkles size={13} strokeWidth={1.75} />}>
        {waitingDraft
          ? `Claude rewrites the draft and follows this for the rest of the conversation. Only ${shortOrgName(us.name)} sees it.`
          : `Claude follows this from its next message in this conversation. Only ${shortOrgName(us.name)} sees it.`}
      </Caption>
    );
  }

  return (
    <div className="relative">
      <p role="status" className="sr-only">
        {suggestions.length > 0 &&
          `${String(suggestions.length)} ${suggestions.length === 1 ? "teammate" : "teammates"} to mention. ` +
            "Up and down to choose, Enter or Tab to insert, Escape to close."}
      </p>
      {suggestions.length > 0 && (
        <div className="absolute bottom-full left-3 z-10 mb-1.5 w-66 rounded-menu border border-border bg-surface p-1.5 shadow-menu">
          <ul id={listId} role="listbox" aria-label="Mention a teammate">
            {suggestions.map((member, index) => (
              <li key={member.id} id={optionId(index)} role="option" aria-selected={index === active}>
                <button
                  type="button"
                  onMouseDown={(event) => {
                    event.preventDefault();
                    insert(member);
                  }}
                  className={cx(
                    "flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-ui-sm",
                    index === active && "bg-hover",
                  )}
                >
                  <Avatar name={member.name} seed={us.slug} size={20} />
                  <span className="truncate">{member.name}</span>
                  <span className="ml-auto text-ui-xs text-muted">@{member.slug}</span>
                </button>
              </li>
            ))}
          </ul>
          <p className="mt-1 border-t border-border px-2 pt-1.5 pb-0.5 text-ui-xs text-muted">
            A mention shares this request with them
          </p>
        </div>
      )}
      <section
        aria-label="Comment or Edit"
        className={cx(
          "rounded-composer border shadow-composer transition-colors",
          editing
            ? "border-info/60 bg-info-soft/40 focus-within:border-info"
            : // Amber while typing, like the comment row it makes.
              "border-border bg-surface focus-within:border-caution/50 focus-within:ring-3 focus-within:ring-caution/15",
        )}
      >
        {composer.note !== null && editing && (
          <p role="status" className="px-4 pt-3 text-ui-sm text-link">
            {composer.note}
          </p>
        )}
        <div className="relative">
          {highlighted && (
            <div
              ref={backdrop}
              aria-hidden
              className={cx(FIELD_BOX, "pointer-events-none absolute inset-0 overflow-y-auto text-ink")}
            >
              {parts.map((part, index) =>
                part.mention ? (
                  <span key={index} className="text-link">
                    {part.text}
                  </span>
                ) : (
                  part.text
                ),
              )}
              {/* A textarea shows the empty line after a final newline; a div needs something on it. */}
              {text.endsWith("\n") && " "}
            </div>
          )}
          <textarea
            ref={field}
            aria-label={editing ? "Tell Claude" : "Comment"}
            aria-autocomplete={editing ? undefined : "list"}
            aria-controls={suggestions.length > 0 ? listId : undefined}
            aria-activedescendant={suggestions.length > 0 ? optionId(active) : undefined}
            rows={1}
            value={text}
            placeholder={PLACEHOLDER[mode]}
            onChange={(event) => {
              write(event.target.value, event.target.selectionStart);
              if (failure !== null && !failure.retry) setFailure(null);
            }}
            onSelect={(event) => {
              setCaret(event.currentTarget.selectionStart);
            }}
            onScroll={(event) => {
              if (backdrop.current !== null) backdrop.current.scrollTop = event.currentTarget.scrollTop;
            }}
            onKeyDown={onKeyDown}
            className={cx(
              FIELD_BOX,
              "relative resize-none bg-transparent outline-none placeholder:text-muted",
              highlighted ? "text-transparent caret-ink" : "text-ink",
            )}
          />
        </div>
        {mentionInEdit && (
          <p className="flex items-center gap-1.5 px-4 pb-1 text-ui-sm text-secondary">
            Mentions are for comments.
            <InlineLink
              onClick={() => {
                switchTo("comment");
              }}
            >
              Switch to Comment?
            </InlineLink>
          </p>
        )}
        <div className="flex items-center gap-2 px-2.5 pt-1 pb-2.5">
          <ModeSwitch mode={mode} editOff={closed} onChange={switchTo} />
          <span className="flex-1" />
          {editing ? (
            <ClaudeButton disabled={!typed || busy || spent !== null} onClick={tellClaude}>
              Tell Claude
            </ClaudeButton>
          ) : (
            <Button variant={typed ? "primary" : "secondary"} disabled={!typed || busy} onClick={comment}>
              Add comment
            </Button>
          )}
        </div>
      </section>
      <div className="mt-2 px-1">{caption}</div>
    </div>
  );
}

const CAPTION_TONE = { muted: "text-muted", info: "text-link", danger: "text-danger" } as const;

function Caption({
  tone,
  icon,
  role,
  children,
}: {
  tone: keyof typeof CAPTION_TONE;
  icon: ReactNode;
  role?: "alert";
  children: ReactNode;
}) {
  return (
    <p role={role} className={cx("flex items-start gap-1.5 text-ui-xs", CAPTION_TONE[tone])}>
      <span aria-hidden className="mt-px flex shrink-0">
        {icon}
      </span>
      <span className="min-w-0">{children}</span>
    </p>
  );
}

function InlineLink({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} className="font-medium text-link hover:underline">
      {children}
    </button>
  );
}

const EDIT_OFF = "Reopen the conversation to tell Claude what to pick up";

/** Comment | Edit, a small track in the box's bottom bar. Edit is off on a closed conversation. */
function ModeSwitch({
  mode,
  editOff,
  onChange,
}: {
  mode: ComposerMode;
  editOff: boolean;
  onChange: (mode: ComposerMode) => void;
}) {
  const segment = (value: ComposerMode, icon: ReactNode, label: string, disabled = false) => {
    const selected = mode === value;
    return (
      <button
        type="button"
        role="radio"
        aria-checked={selected}
        aria-disabled={disabled || undefined}
        onClick={() => {
          if (!disabled && !selected) onChange(value);
        }}
        className={cx(
          "inline-flex h-7 items-center gap-1.5 rounded-[6px] px-2.5 text-ui font-medium transition-colors",
          selected && value === "comment" && "bg-surface text-ink shadow-[0_0_0_0.5px_var(--border-strong),0_1px_2px_rgb(20_20_19/0.06)]",
          selected && value === "edit" && "bg-surface text-link shadow-[0_0_0_0.5px_var(--info),0_1px_2px_rgb(20_20_19/0.06)]",
          !selected && !disabled && "text-secondary hover:text-ink",
          disabled && "cursor-default text-faint",
        )}
      >
        {icon}
        {label}
      </button>
    );
  };
  const edit = segment("edit", <Sparkles size={14} strokeWidth={1.75} aria-hidden />, "Edit", editOff);
  return (
    <div role="radiogroup" aria-label="Mode" className="inline-flex h-8 items-center gap-0.5 rounded-control bg-hover p-0.5">
      {segment("comment", <MessageSquare size={14} strokeWidth={1.75} aria-hidden className="text-caution" />, "Comment")}
      {editOff ? (
        <Tooltip content={EDIT_OFF} side="top">
          {edit}
        </Tooltip>
      ) : (
        edit
      )}
    </div>
  );
}
