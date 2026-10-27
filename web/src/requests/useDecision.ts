/**
 * One open item's decision, shared by the card that shows it and the composer that acts
 * on it (LIVE_DEMO_PLAN 3.9): the draft rev shown and the member's edit of it, and the
 * resolve and redraft calls with what came of them.
 *
 * A redraft lands as a new rev. Until it does, the shown draft is marked as being
 * redrafted and can't be approved; a redraft the worker reports failed, or that never
 * lands, stops the wait and says why, aware of a spent live-model budget. A redraft or
 * send-back the server didn't take keeps its instruction for Try again: the composer
 * already stored it as a note, so retrying asks again without storing it twice.
 *
 * Approve, decline, redraft and send-back each hand the move to a model, whose call a
 * spent cap or a pause would refuse, ending the thread for good (or wasting the
 * redraft): while the budget isn't `ok` they hold, and `blocked` says why. Editing the
 * draft and adding notes call no model, so they still work.
 *
 * The view keeps one decision for as long as the thread is open, so the composer under
 * it keeps what the member typed when an item opens, resolves or gives way to another.
 * What the member did with an item stays with that item.
 */
import { useEffect, useState } from "react";

import { budgetLine, useBudgetState } from "../chat/failureCopy";
import { toast } from "../shell/toasts";
import { problemOf, useRedraftEscalation, useResolveEscalation } from "./api";
import { approveBody, declineBody, isEdited, shownAnswer, sortedDrafts, type DraftEdits } from "./draft";
import { redraftFailure } from "./failure";
import { representativeName } from "./format";
import { useFailedRedrafts } from "./live";
import type { ConsultView, EscalationDraft, EscalationView, OrgRef, ResolveEscalation } from "./types";

/**
 * How long a redraft may take before the view stops waiting and lets the member retry.
 * A redraft that fails says so at once; this covers a report the stream dropped, and
 * leaves room for a live answerer's loop.
 */
export const REDRAFT_TIMEOUT_MS = 180_000;

export interface Notice {
  tone: "error" | "info";
  text: string;
  action?: { label: string; run: () => void };
}

export interface Decision {
  escalation: EscalationView;
  consult: ConsultView | null;
  drafts: readonly EscalationDraft[];
  /** The rev shown: the newest unless the member paged back. */
  index: number;
  rev: EscalationDraft | null;
  showRev: (index: number) => void;
  /** The shown draft as it stands after the member's edit. */
  answer: string;
  edited: boolean;
  setAnswer: (value: string) => void;
  editing: boolean;
  setEditing: (editing: boolean) => void;
  redrafting: boolean;
  /** A resolve or redraft call is in flight. */
  busy: boolean;
  /** Why live turns can't start now (`budgetLine`): every action that starts one holds. */
  blocked: string | null;
  notice: Notice | null;
  clearNotice: () => void;
  approve: () => void;
  decline: (reason: string, onDone: () => void) => void;
  /** Ask the answerer for a new draft (a consult); refused, with a notice, while one is on its way. */
  redraft: (instruction: string) => void;
  /** Send a held message back to the representative with what to change. */
  revise: (instruction: string) => void;
}

interface Waiting {
  rev: number;
  instruction: string;
}

/** A redraft or send-back the server didn't take, kept for Try again. */
interface Unsent {
  action: "redraft" | "revise";
  instruction: string;
  /** The server was still running another redraft of the item (a teammate's, say). */
  queued: boolean;
}

/** What the member did with one item. */
interface ItemState {
  /** The rev paged back to; null follows the newest. */
  revIndex: number | null;
  editsByRev: Readonly<Record<number, DraftEdits>>;
  editing: boolean;
  notice: Notice | null;
  waiting: Waiting | null;
  /** The instruction of a redraft that never landed. */
  timedOut: string | null;
  unsent: Unsent | null;
}

const FRESH: ItemState = {
  revIndex: null,
  editsByRev: {},
  editing: false,
  notice: null,
  waiting: null,
  timedOut: null,
  unsent: null,
};

const STILL_REDRAFTING = "Claude is still working on an earlier redraft. Try again once it lands.";
const NOT_SENT_BACK = "Couldn't send this back to Claude. Your note is saved; nothing went out.";

type ItemPatch = Partial<ItemState> | ((current: ItemState) => Partial<ItemState>);

function patched(
  items: Readonly<Record<string, ItemState>>,
  itemId: string,
  patch: ItemPatch,
): Readonly<Record<string, ItemState>> {
  const before = items[itemId] ?? FRESH;
  return { ...items, [itemId]: { ...before, ...(typeof patch === "function" ? patch(before) : patch) } };
}

function successToast(body: ResolveEscalation, escalation: EscalationView, us: OrgRef, them: OrgRef): string {
  const consult = escalation.kind === "consult";
  switch (body.action) {
    case "approve":
      return consult ? `Released to ${representativeName(us.name)}` : `Sent to ${them.name}`;
    case "revise":
      return "Sent back to Claude. Nothing went out.";
    default:
      return consult ? `Declined. Claude will let ${them.name} know.` : "Declined. The reply won't be sent.";
  }
}

export function useDecision(
  escalation: EscalationView | null,
  consult: ConsultView | null,
  threadId: string,
  us: OrgRef,
  them: OrgRef,
): Decision | null {
  const itemId = escalation?.id ?? "";
  const target = { threadId, escalationId: itemId };
  const resolve = useResolveEscalation(target);
  const redraftCall = useRedraftEscalation(target);
  const budget = useBudgetState();
  const blocked = budgetLine(budget);
  const failedRevs = useFailedRedrafts(itemId);

  // Keyed by item, so a call that settles after its item went updates that item only.
  const [items, setItems] = useState<Readonly<Record<string, ItemState>>>({});
  const state = items[itemId] ?? FRESH;
  const update = (patch: ItemPatch) => {
    setItems((current) => patched(current, itemId, patch));
  };

  const drafts = sortedDrafts(escalation?.drafts ?? []);
  const index = state.revIndex ?? drafts.length - 1;
  const rev = drafts[index] ?? null;
  const edits = state.editsByRev[rev?.draft_rev ?? 0] ?? {};
  const { waiting, timedOut, unsent } = state;

  const newestRev = drafts.at(-1)?.draft_rev ?? null;
  const failed = waiting !== null && failedRevs.has(waiting.rev) ? waiting : null;
  const redrafting = waiting !== null && failed === null && (newestRev === null || newestRev < waiting.rev);
  const busy = resolve.isPending || redraftCall.isPending;

  useEffect(() => {
    if (!redrafting) return;
    const timer = window.setTimeout(() => {
      setItems((current) => patched(current, itemId, { waiting: null, timedOut: waiting.instruction }));
    }, REDRAFT_TIMEOUT_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [redrafting, waiting, itemId]);

  if (escalation === null) return null;

  const fail = (error: unknown) => {
    const problem = problemOf(error);
    if (problem?.code === "escalation_not_open") {
      // `resolved_by` is set only when someone resolved it; a redraft already running
      // or a cancelled item comes back without it.
      update({
        notice:
          problem.resolved_by == null
            ? { tone: "info", text: "This item changed while you were deciding. Check the latest before trying again." }
            : { tone: "info", text: `Already resolved by ${problem.resolved_by.name}.` },
      });
      return;
    }
    update({
      notice: { tone: "error", text: problem?.detail ?? (error instanceof Error ? error.message : "Something went wrong.") },
    });
  };

  const clearNotice = () => {
    update({ notice: null, timedOut: null, unsent: null });
  };

  const redraft = (instruction: string) => {
    if (redrafting) {
      update({ notice: { tone: "info", text: STILL_REDRAFTING } });
      return;
    }
    if (busy || blocked !== null) return;
    clearNotice();
    redraftCall.mutate(instruction, {
      onSuccess: (started) => {
        update({ waiting: { rev: started.draft_rev, instruction }, revIndex: null, editing: false });
      },
      onError: (error) => {
        const problem = problemOf(error);
        if (problem?.code === "escalation_not_open" && problem.resolved_by != null) {
          fail(error);
          return;
        }
        update({ unsent: { action: "redraft", instruction, queued: problem?.code === "escalation_not_open" } });
      },
    });
  };

  const send = (body: ResolveEscalation, onDone?: () => void) => {
    clearNotice();
    resolve.mutate(body, {
      onSuccess: () => {
        toast(successToast(body, escalation, us, them));
        update({ editing: false });
        onDone?.();
      },
      onError: (error) => {
        if (body.action === "revise" && problemOf(error)?.code !== "escalation_not_open") {
          update({ unsent: { action: "revise", instruction: body.text ?? "", queued: false } });
          return;
        }
        fail(error);
      },
    });
  };

  const revise = (instruction: string) => {
    if (busy || blocked !== null) return;
    send({ action: "revise", text: instruction, draft_rev: rev?.draft_rev ?? null });
  };

  // What a redraft that came to nothing says, with a retry while one could work.
  const stopped = (how: "failed" | "timeout", instruction: string): Notice => {
    const copy = redraftFailure(how, budget);
    if (!copy.retry) return { tone: "error", text: copy.text };
    const run = () => {
      update({ waiting: null });
      redraft(instruction);
    };
    return { tone: "error", text: copy.text, action: { label: "Try again", run } };
  };
  // What a call the server didn't take says, keeping its instruction for a retry.
  const notTaken = ({ action, instruction, queued }: Unsent): Notice => {
    if (action === "redraft" && !queued) return stopped("failed", instruction);
    const run = () => {
      if (action === "revise") revise(instruction);
      else redraft(instruction);
    };
    const [tone, text] = action === "revise" ? (["error", NOT_SENT_BACK] as const) : (["info", STILL_REDRAFTING] as const);
    return { tone, text, action: { label: "Try again", run } };
  };
  let shownNotice = state.notice;
  if (failed !== null) shownNotice = stopped("failed", failed.instruction);
  else if (timedOut !== null) shownNotice = stopped("timeout", timedOut);
  else if (unsent !== null) shownNotice = notTaken(unsent);

  return {
    escalation,
    consult,
    drafts,
    index,
    rev,
    showRev: (next) => {
      update({ revIndex: next === drafts.length - 1 ? null : next, editing: false });
    },
    answer: shownAnswer(rev, edits),
    edited: isEdited(edits),
    setAnswer: (value) => {
      const key = rev?.draft_rev ?? 0;
      update((current) => ({ editsByRev: { ...current.editsByRev, [key]: { answer: value } } }));
    },
    editing: state.editing && !redrafting,
    setEditing: (value) => {
      update({ editing: value && !redrafting });
    },
    redrafting,
    busy,
    blocked,
    notice: shownNotice,
    clearNotice,
    approve: () => {
      if (busy || redrafting || blocked !== null) return;
      const built = approveBody(escalation.kind, rev, edits);
      if (!built.ok) {
        update({ notice: { tone: "error", text: built.error } });
        return;
      }
      send(built.body);
    },
    decline: (reason, onDone) => {
      if (busy || blocked !== null) return;
      send(declineBody(reason, rev), onDone);
    },
    redraft,
    revise,
  };
}
