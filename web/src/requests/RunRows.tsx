/**
 * How each run of the conversation ended and how the next began (REOPEN.md §8): a
 * centred Closed row per run with its reason and our summary, the latest carrying
 * Reopen; the small blue prompt Reopen opens, since what's typed goes to our own Claude
 * and never to the other org; and the reopen rows, ours with what was typed and how it
 * went, the counterparty's naming only its org.
 */
import { CircleAlert, CircleCheck, Clock, CornerUpLeft, Loader2, Lock } from "lucide-react";
import { useEffect, type ReactNode } from "react";

import { budgetLine } from "../chat/failureCopy";
import { useCalendar } from "../demo/worldClock";
import { firstName, shortOrgName } from "../shell/names";
import { Button } from "../shell/ui";
import { ClaudeButton } from "./ClaudeButton";
import { EventRow, LinkButton, Said, When, Who } from "./EventRow";
import type { RowActions } from "./EventRows";
import { useAutoHeight } from "./autoHeight";
import { blockLine, reopenFailure } from "./failure";
import { endLine, possessive, representativeName, shortDate } from "./format";
import type { EndedEntry, ReopenEntry } from "./timeline";
import type { OrgRef } from "./types";
import type { Reopen } from "./useReopen";

interface EndedRowProps {
  entry: EndedEntry;
  us: OrgRef;
  them: OrgRef;
  actions: RowActions;
  /** An attempt of ours to reopen came after this run ended: its row carries the next step. */
  attempted: boolean;
}

/** "Closed · Oct 29 · both sides went quiet", our summary of that run, and, on the latest, Reopen. */
export function EndedRow({ entry, us, them, actions, attempted }: EndedRowProps) {
  const calendar = useCalendar();
  const why = endLine(entry.ending, actions.side, them.name, actions.budget);
  const ours = shortOrgName(us.name);
  const prompting = actions.reopen.prompt?.anchor === entry.id;
  return (
    <section
      aria-label={`Closed ${shortDate(entry.at, calendar)}`}
      className="flex flex-col items-center gap-2 pt-2 pb-1 text-center"
    >
      <p className="text-ui-sm text-muted">
        <CircleCheck size={15} strokeWidth={1.75} aria-hidden className="mr-1.5 inline-block align-[-3px]" />
        <span className="font-medium text-secondary">Closed</span> · <When at={entry.at} date />
        {why !== null && <> · {why}</>}
      </p>
      {entry.summary !== null && entry.summary !== "" && (
        <div className="w-full max-w-[560px] rounded-[10px] border border-border bg-surface px-3 py-2.5 text-left text-ui-sm text-secondary">
          <p className="break-words whitespace-pre-wrap">{entry.summary}</p>
          <p className="mt-1 text-ui-xs text-muted">
            {possessive(representativeName(ours))} summary. Only {ours} sees it.
          </p>
        </div>
      )}
      {entry.latest &&
        !attempted &&
        (prompting ? (
          <ReopenPrompt us={us} them={them} reopen={actions.reopen} held={reopenHeld(actions)} />
        ) : (
          <ReopenOffered anchor={entry.id} us={us} them={them} actions={actions} />
        ))}
    </section>
  );
}

function Line({ icon, children }: { icon: ReactNode; children: string }) {
  return (
    <p className="text-ui-sm text-muted">
      <span aria-hidden className="mr-1.5 inline-block align-[-3px]">
        {icon}
      </span>
      {children}
    </p>
  );
}

/** What the latest Closed row shows: Reopen, Reopen held back and why, or why it can't be reopened. */
function ReopenOffered({ anchor, us, them, actions }: { anchor: string; us: OrgRef; them: OrgRef; actions: RowActions }) {
  const { reopenOffer, reopen } = actions;
  if (reopenOffer === null) return null;
  if (reopenOffer.state === "blocked") {
    const line = reopenOffer.block === null ? null : blockLine(reopenOffer.block, shortOrgName(us.name), them.name);
    return line === null ? null : <Line icon={<Lock size={14} strokeWidth={1.75} />}>{line}</Line>;
  }
  const wrapping = reopenOffer.state === "wrapping_up";
  const held = reopenHeld(actions);
  return (
    <>
      <Button
        icon={<CornerUpLeft size={15} strokeWidth={1.75} aria-hidden />}
        disabled={held !== null}
        onClick={() => {
          reopen.open(anchor);
        }}
      >
        Reopen
      </Button>
      {held !== null && (
        <Line icon={wrapping ? <Clock size={14} strokeWidth={1.75} /> : <CircleAlert size={14} strokeWidth={1.75} />}>
          {held}
        </Line>
      )}
    </>
  );
}

/** Why Reopen waits for now, if it does: Claude is still wrapping up, or the budget is spent. */
function reopenHeld({ reopenOffer, budget }: RowActions): string | null {
  return reopenOffer?.state === "wrapping_up" ? "Claude is still wrapping up this conversation." : budgetLine(budget);
}

/**
 * The prompt Reopen opens where the button was: what should Claude pick up? It can be
 * open while Reopen waits (an Edit left in the box when the conversation closed moves
 * here), and then holds the text with the reason.
 */
function ReopenPrompt({ us, them, reopen, held }: { us: OrgRef; them: OrgRef; reopen: Reopen; held: string | null }) {
  const text = reopen.prompt?.text ?? "";
  const field = useAutoHeight(text, 240);
  const ready = text.trim() !== "" && !reopen.pending && held === null;
  // The prompt opens with its text (a Try again's) ready to change, the caret at its end.
  useEffect(() => {
    const element = field.current;
    if (element === null) return;
    element.focus();
    element.setSelectionRange(element.value.length, element.value.length);
  }, [field]);
  return (
    <section
      aria-label={`Reopen with ${them.name}`}
      className="w-full max-w-[560px] rounded-[12px] border border-info/70 bg-info-soft/40 p-3 text-left"
    >
      <h3 className="flex items-center gap-1.5 text-ui-sm font-medium text-link">
        <CornerUpLeft size={15} strokeWidth={1.75} aria-hidden />
        Reopen with {them.name}
      </h3>
      <textarea
        ref={field}
        aria-label="What should Claude pick up?"
        placeholder="What should Claude pick up?"
        rows={2}
        value={text}
        readOnly={reopen.pending}
        onChange={(event) => {
          reopen.setText(event.target.value);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            reopen.cancel();
          } else if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            if (ready) reopen.submit();
          }
        }}
        className="mt-1.5 block min-h-11 w-full resize-none bg-transparent text-ui text-ink outline-none placeholder:text-muted"
      />
      {reopen.error !== null && (
        <p role="alert" className="mt-1 text-ui-sm text-danger">
          {reopen.error.text}
        </p>
      )}
      {held !== null && reopen.error === null && <p className="mt-1 text-ui-sm text-muted">{held}</p>}
      <div className="mt-2 flex items-center gap-2">
        <p className="min-w-0 flex-1 text-ui-xs text-link">
          Claude writes the next message, following your usual review settings. Only {shortOrgName(us.name)} sees what you
          typed.
        </p>
        <Button variant="ghost" onClick={reopen.cancel}>
          Cancel
        </Button>
        <ClaudeButton disabled={!ready} onClick={reopen.submit}>
          {reopen.pending && <Loader2 size={14} strokeWidth={2} aria-hidden className="animate-spin" />}
          Reopen
        </ClaudeButton>
      </div>
    </section>
  );
}

interface ReopenRowProps {
  entry: ReopenEntry;
  us: OrgRef;
  them: OrgRef;
  actions: RowActions;
  /** The latest failed attempt on a still-closed thread: it shows Try again. */
  retryable: boolean;
}

const VERB = { working: "is reopening", opened: "reopened", failed: "tried to reopen" } as const;

/**
 * A reopen: ours with who, what they typed and how it went (picking up, reopened, or
 * failed, which changed nothing anyone else saw); the counterparty's with only its org.
 */
export function ReopenRow({ entry, us, them, actions, retryable }: ReopenRowProps) {
  const icon = <CornerUpLeft size={15} strokeWidth={1.75} />;
  if (!entry.ours) {
    return (
      <EventRow
        icon={icon}
        label={`${entry.org.name} reopened the conversation`}
        header={
          <>
            <Who>{entry.org.name}</Who> reopened the conversation · <When at={entry.at} date />
          </>
        }
      />
    );
  }
  const who = entry.by === null ? "A teammate" : firstName(entry.by.name);
  const verb = VERB[entry.outcome];
  const { reopen, budget } = actions;
  const body = entry.steer?.body ?? null;
  const failure = entry.outcome === "failed" ? reopenFailure(entry.steer?.failure_reason ?? null, budget) : null;
  const prompting = reopen.prompt?.anchor === entry.id;
  return (
    <div className="flex flex-col items-center gap-2">
      <div className="w-full">
        <EventRow
          icon={icon}
          label={`${who} ${verb} the conversation`}
          header={
            <>
              <Who>{who}</Who> {verb} the conversation · <When at={entry.at} date />
            </>
          }
        >
          {body !== null && <Said>{body}</Said>}
          {entry.outcome === "working" && (
            <p className="inline-flex items-center gap-1.5 text-link">
              <Loader2 size={13} strokeWidth={2} aria-hidden className="animate-spin" />
              Claude is picking this up…
            </p>
          )}
          {entry.outcome === "opened" && body !== null && (
            <p>
              Claude updated its guidance and writes the next message.{" "}
              <LinkButton onClick={actions.onSeeGuidance}>See guidance</LinkButton>
            </p>
          )}
          {failure !== null && (
            <p>
              <span className="text-warning">{failure.text}</span> Nothing was sent, and {them.name} didn't see this. Your
              text is saved.
              {retryable && failure.retry && !prompting && (
                <>
                  {" "}
                  <LinkButton
                    onClick={() => {
                      reopen.open(entry.id, body ?? "");
                    }}
                  >
                    Try again
                  </LinkButton>
                </>
              )}
            </p>
          )}
        </EventRow>
      </div>
      {prompting && <ReopenPrompt us={us} them={them} reopen={reopen} held={reopenHeld(actions)} />}
    </div>
  );
}
