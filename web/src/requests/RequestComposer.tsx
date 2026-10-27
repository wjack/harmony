/**
 * The request view's one composer (LIVE_DEMO_PLAN 3.9). With nothing open on the thread
 * it adds internal notes; with an open item it also acts on it. Typed text is always
 * stored as a note first (our org and the answerer read notes), then, for a consult,
 * asks the answerer to redraft with it, or, for a held message, sends the message back
 * to the representative with it. Text that mentions a teammate is only a note: the
 * mention shares the thread with them (by the server's rule, `mentions.ts`).
 *
 * Decline is a step of its own, because its reason may reach the counterparty: the note
 * text is set aside (Cancel, or the decline going through, restores it) and never reused
 * as a reason.
 *
 * The composer outlives the items it acts on, so what the member typed survives one
 * opening or resolving. A Decline step whose item went ends as Cancel does.
 *
 * While the budget stops live turns, the actions that start one (approve, decline,
 * redraft, send back) hold, and the chat composer's line says why on the top edge;
 * notes and editing the draft still work (`Decision.blocked`).
 */
import { useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode, type Ref } from "react";

import { BudgetStrip } from "../chat/BudgetStrip";
import { Avatar, Button, cx } from "../shell/ui";
import { problemOf, useAddNote } from "./api";
import { useAutoHeight } from "./autoHeight";
import { approvalCaption } from "./decision";
import { mentionAt, mentionsMember } from "./mentions";
import type { MemberView, OrgRef } from "./types";
import type { Decision } from "./useDecision";

const MAX_FIELD_PX = 220;

interface RequestComposerProps {
  threadId: string;
  us: OrgRef;
  them: OrgRef;
  /** Our org's members, for @mentions. */
  members: readonly MemberView[];
  memberId: string | undefined;
  /** The open item, when there is one. */
  decision: Decision | null;
}

export function RequestComposer({ threadId, us, them, members, memberId, decision }: RequestComposerProps) {
  const addNote = useAddNote(threadId);
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [highlight, setHighlight] = useState(0);
  /** The note text set aside while the member writes a decline reason. */
  const [aside, setAside] = useState<string | null>(null);
  const root = useRef<HTMLDivElement | null>(null);

  // A Decline step belongs to its item: once that item goes, it ends as Cancel does.
  const item = decision?.escalation.id ?? null;
  const [shownItem, setShownItem] = useState(item);
  if (shownItem !== item) {
    setShownItem(item);
    if (aside !== null) {
      setText(aside);
      setCaret(aside.length);
      setAside(null);
    }
  }

  const declining = aside !== null;
  let placeholder = "Add an internal note… Use @ to bring in a teammate";
  let label = "Internal note";
  if (declining) {
    placeholder = `The reason Claude can give ${them.name} (optional)…`;
    label = "Reason for declining";
  } else if (decision !== null) {
    placeholder = "Tell Claude what to change…";
    label = "Instruction or note";
  }

  const field = useAutoHeight(text, MAX_FIELD_PX, placeholder);
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

  /**
   * Editing can end in the draft's own field (Esc there), which then goes and takes
   * focus with it: focus comes back to Edit, or to this field while typed text hides it.
   */
  const editButton = useRef<HTMLButtonElement | null>(null);
  const editing = decision?.editing === true;
  const wasEditing = useRef(editing);
  useLayoutEffect(() => {
    const lost = document.activeElement === null || document.activeElement === document.body;
    if (wasEditing.current && !editing && lost) (editButton.current ?? field.current)?.focus();
    wasEditing.current = editing;
  }, [editing, field]);

  const typed = text.trim() !== "";
  const noteOnly = decision === null || mentionsMember(text, members);
  const busy = addNote.isPending || (decision?.busy ?? false);
  const blocked = decision?.blocked ?? null;
  const held = decision?.escalation.kind === "outbound_review";

  // Esc closes the mention list where it was opened; typing or moving the caret reopens it.
  const [dismissedAt, setDismissedAt] = useState<string | null>(null);
  const place = `${String(caret)}:${text}`;
  const mention = declining || dismissedAt === place ? null : mentionAt(text, caret);
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

  const write = (value: string, at = value.length) => {
    setText(value);
    setCaret(at);
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

  /**
   * Whatever in the composer last had focus. A state change can take it away (Approve
   * and send goes with the item it approved): focus then comes back to the field rather
   * than falling to the page, unless the member has moved on meanwhile.
   */
  const lastFocused = useRef<Element | null>(null);
  useLayoutEffect(() => {
    const last = lastFocused.current;
    if (last === null || last.isConnected) return;
    lastFocused.current = null;
    if (document.activeElement === null || document.activeElement === document.body) field.current?.focus();
  });

  /**
   * Back to the field once a pressed button has gone with the state it belonged to,
   * unless the member has since moved on to something outside the composer.
   */
  const refocus = () => {
    const active = document.activeElement;
    if (active === null || active === document.body || root.current?.contains(active) === true) field.current?.focus();
  };

  /** Stores the text as a note, then hands it on (to redraft or send back) once stored. */
  const note = (then?: (body: string) => void) => {
    const body = text.trim();
    if (body === "" || busy) return;
    decision?.clearNotice();
    addNote.mutate(
      { body },
      {
        onSuccess: () => {
          write("");
          refocus();
          then?.(body);
        },
      },
    );
  };

  const act = () => {
    if (decision === null || noteOnly) {
      note();
    } else if (blocked !== null) {
      // As the disabled button: nothing is stored, and the text stays for later.
    } else if (decision.redrafting && !held) {
      // As the disabled Redraft button: nothing is stored, and the text stays for later.
      decision.redraft(text.trim());
    } else if (held) {
      note((body) => {
        decision.revise(body);
      });
    } else {
      note((body) => {
        decision.redraft(body);
      });
    }
  };

  const startDecline = () => {
    decision?.clearNotice();
    setAside(text);
    write("");
    field.current?.focus();
  };

  const cancelDecline = () => {
    write(aside ?? "");
    setAside(null);
    refocus();
  };

  const decline = () => {
    decision?.decline(text, () => {
      write(aside ?? "");
      setAside(null);
      refocus();
    });
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
        const member = suggestions[active];
        if (member !== undefined) insert(member);
        return;
      }
      if (event.key === "Escape") {
        // Only the list closes: not the Decline step, not editing the draft.
        event.preventDefault();
        setDismissedAt(place);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      const command = event.metaKey || event.ctrlKey;
      if (declining) decline();
      else if (typed) act();
      // ⌘↵ with nothing typed approves the draft as shown, edited or not.
      else if (command) decision?.approve();
    } else if (event.key === "Escape") {
      if (declining) {
        event.preventDefault();
        cancelDecline();
      } else if (decision?.editing === true) {
        decision.setEditing(false);
      }
    }
  };

  const error = addNote.error === null ? null : (problemOf(addNote.error)?.detail ?? addNote.error.message);
  const notice = decision?.notice ?? null;

  let caption = `Only ${us.name} sees notes`;
  if (declining) caption = `Claude may pass this reason on to ${them.name} · Esc to cancel`;
  else if (decision !== null) caption = approvalCaption(decision.escalation, them, memberId, typed);

  return (
    <div
      ref={root}
      className="relative"
      onFocus={(event) => {
        lastFocused.current = event.target;
      }}
    >
      {blocked !== null && <BudgetStrip line={blocked} />}
      <p role="status" className="sr-only">
        {suggestions.length > 0 &&
          `${String(suggestions.length)} ${suggestions.length === 1 ? "teammate" : "teammates"} to mention. ` +
            "Up and down to choose, Enter or Tab to insert, Escape to close."}
      </p>
      {suggestions.length > 0 && (
        <ul
          id={listId}
          role="listbox"
          aria-label="Mention a teammate"
          className="absolute bottom-full left-3 z-10 mb-1.5 w-64 rounded-menu border border-border bg-surface p-1.5 shadow-menu"
        >
          {suggestions.map((member, index) => (
            <li key={member.id} id={optionId(index)} role="option" aria-selected={index === active}>
              <button
                type="button"
                onMouseDown={(event) => {
                  event.preventDefault();
                  insert(member);
                }}
                className={cx(
                  "flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-[13px]",
                  index === active && "bg-hover",
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
      <section
        aria-label={decision === null ? "Add a note" : "Your decision"}
        className="relative rounded-composer border border-border bg-surface shadow-composer transition-[border-color] focus-within:border-border-strong"
      >
        {notice !== null && (
          <div
            role={notice.tone === "error" ? "alert" : "status"}
            className={cx(
              "mx-3 mt-3 flex items-center gap-3 rounded-control px-3 py-2 text-[13px] leading-5",
              notice.tone === "error" ? "bg-danger-soft text-danger" : "bg-info-soft text-info",
            )}
          >
            <span className="flex-1">{notice.text}</span>
            {notice.action !== undefined && (
              <Button size="sm" variant="secondary" onClick={notice.action.run}>
                {notice.action.label}
              </Button>
            )}
          </div>
        )}
        {/* One row, the buttons after the field, until the field is too narrow (a phone):
            then the buttons wrap under it, still at the right. */}
        <div className="flex flex-wrap items-end gap-x-2 p-1.5">
          <textarea
            ref={field}
            aria-label={label}
            aria-autocomplete="list"
            aria-controls={suggestions.length > 0 ? listId : undefined}
            aria-activedescendant={suggestions.length > 0 ? optionId(active) : undefined}
            rows={1}
            value={text}
            placeholder={placeholder}
            onChange={(event) => {
              write(event.target.value, event.target.selectionStart);
            }}
            onSelect={(event) => {
              setCaret(event.currentTarget.selectionStart);
            }}
            onKeyDown={onKeyDown}
            className="scrollbar-quiet block min-h-10 min-w-0 flex-[1_1_240px] resize-none bg-transparent px-3 py-2 text-[15px] leading-6 text-ink outline-none placeholder:text-muted"
          />
          <Actions
            decision={decision}
            declining={declining}
            typed={typed}
            noteOnly={noteOnly}
            held={held}
            busy={busy}
            blocked={blocked !== null}
            onNote={() => {
              note();
            }}
            onAct={act}
            onDecline={startDecline}
            onCancelDecline={cancelDecline}
            onConfirmDecline={decline}
            editRef={editButton}
          />
        </div>
        {error !== null && (
          <p role="alert" className="px-[18px] pb-2.5 text-[12.5px] leading-5 text-danger">
            {error}
          </p>
        )}
      </section>
      <p className="mt-2 text-center text-[12px] leading-4 text-muted">
        {decision?.edited === true && !declining ? "Edited · " : ""}
        {caption}
      </p>
    </div>
  );
}

interface ActionsProps {
  decision: Decision | null;
  declining: boolean;
  typed: boolean;
  noteOnly: boolean;
  held: boolean;
  busy: boolean;
  /** The budget stops live turns: the buttons that start one are disabled. */
  blocked: boolean;
  onNote: () => void;
  onAct: () => void;
  onDecline: () => void;
  onCancelDecline: () => void;
  onConfirmDecline: () => void;
  editRef: Ref<HTMLButtonElement>;
}

/**
 * The buttons for the composer's state (the states table in 3.9), at the row's end.
 * Wider than a phone's composer, they wrap onto a second line, still at the right.
 */
function Actions(props: ActionsProps) {
  const buttons = actionButtons(props);
  if (buttons === null) return null;
  return (
    <div className="ml-auto flex min-h-10 max-w-full flex-wrap items-center justify-end gap-2">{buttons}</div>
  );
}

/**
 * Keyed: a state change swaps the buttons, and a button must not inherit another's
 * focus or press (Decline turning into Cancel under the pointer).
 */
function actionButtons({
  decision,
  declining,
  typed,
  noteOnly,
  held,
  busy,
  blocked,
  onNote,
  onAct,
  onDecline,
  onCancelDecline,
  onConfirmDecline,
  editRef,
}: ActionsProps): ReactNode {
  const declineButton = (
    <Button key="decline" size="sm" variant="secondary" disabled={busy || blocked} onClick={onDecline}>
      Decline
    </Button>
  );
  if (declining) {
    return [
      <Button key="cancel" size="sm" variant="ghost" disabled={busy} onClick={onCancelDecline}>
        Cancel
      </Button>,
      <Button key="confirm-decline" size="sm" variant="danger" disabled={busy || blocked} onClick={onConfirmDecline}>
        Decline
      </Button>,
    ];
  }
  if (typed) {
    if (decision === null || noteOnly) {
      return (
        <Button key="note" size="sm" variant="primary" disabled={busy} onClick={onNote}>
          Add note
        </Button>
      );
    }
    // Decline stays: it sets the typed note aside for its own reason field.
    return [
      declineButton,
      <Button key="note" size="sm" variant="secondary" disabled={busy} onClick={onNote}>
        Add note
      </Button>,
      <Button
        key="act"
        size="sm"
        variant="primary"
        disabled={busy || blocked || (!held && decision.redrafting)}
        onClick={onAct}
      >
        {held ? "Send back to Claude" : "Redraft"}
      </Button>,
    ];
  }
  if (decision === null) return null;
  return [
    declineButton,
    decision.redrafting ? null : (
      <Button
        key="edit"
        ref={editRef}
        size="sm"
        variant="secondary"
        disabled={busy}
        onClick={() => {
          decision.setEditing(!decision.editing);
        }}
      >
        {decision.editing ? "Done" : "Edit"}
      </Button>
    ),
    <Button
      key="approve"
      size="sm"
      variant="primary"
      disabled={busy || blocked || decision.redrafting}
      onClick={decision.approve}
    >
      Approve and send
    </Button>,
  ];
}
