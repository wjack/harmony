/**
 * One open item's decision, shared by the card that shows it and the composer whose Edit
 * can rewrite it (EDIT.md §8.3): the draft rev shown and the member's edit of it, the
 * resolve calls with what came of them, and the wait for a rewrite.
 *
 * An Edit on an open item reserves the item's next rev (`SteerStarted.draft_rev`). Until
 * that rev lands, the draft is locked under "Claude is rewriting…" and Approve waits, so
 * nobody approves the old wording by mistake; Decline still works. The wait ends when
 * the rev lands, when the steer reports the rewrite failed or settled, or after
 * `REDRAFT_TIMEOUT_MS`, a fallback for a report the stream dropped. A teammate's Edit
 * locks the draft the same way: the steer list says a rev is reserved.
 *
 * When Claude declined the step the item waits on (`EscalationView.refusal`), Retry runs
 * it again as the next rev, on the member's edit of the draft if there is one. Approve
 * waits until that rev lands or Claude declines it too, with the same fallback timeout.
 *
 * Approve, decline and Retry hand the move to a model, whose call a spent cap or a pause
 * would refuse: while the budget isn't `ok` they hold, and `blocked` says why. Editing
 * the wording calls no model, so it still works.
 *
 * What the member did with an item stays with that item.
 */
import { useEffect, useState } from "react";

import type { SteerView } from "../api/types";
import { budgetLine, useBudgetState } from "../chat/failureCopy";
import { toast } from "../shell/toasts";
import { problemOf, useResolveEscalation, useRetryEscalation } from "./api";
import { approveBody, declineBody, isEdited, shownAnswer, sortedDrafts, type DraftEdits } from "./draft";
import type { Refusal } from "./decision";
import { representativeName } from "./format";
import type { ConsultView, EscalationDraft, EscalationView, OrgRef, ResolveEscalation } from "./types";

/**
 * How long a rewrite may take before the card stops waiting. A rewrite that fails says
 * so at once through its steer; this covers a report the stream dropped, and leaves
 * room for a live answerer's loop.
 */
export const REDRAFT_TIMEOUT_MS = 180_000;

export interface Notice {
  tone: "error" | "info";
  text: string;
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
  undoEdits: () => void;
  /** An Edit reserved the next rev and it hasn't landed: the draft is locked and Approve waits. */
  rewriting: boolean;
  /** The latest Edit's rewrite came to nothing: the draft is as it was. */
  unchanged: boolean;
  /** Claude declined the step the item waits on, while no Retry of it is under way. */
  refusal: Refusal | null;
  /** A Retry reserved a rev that hasn't landed (or been declined): Approve waits. */
  retrying: boolean;
  /** Run the declined step again: on the member's edit of the shown draft, if any. */
  retry: () => void;
  /** An Edit of ours reserved `rev` for this item. */
  awaitRewrite: (rev: number, steerId: string) => void;
  /** A resolve or Retry call is in flight. */
  busy: boolean;
  /** Why live turns can't start now (`budgetLine`): approving and declining hold. */
  blocked: string | null;
  notice: Notice | null;
  clearNotice: () => void;
  approve: () => void;
  decline: (reason: string | null, onDone: () => void) => void;
}

interface Waiting {
  rev: number;
  steerId: string;
}

/** What the member did with one item. */
interface ItemState {
  /** The rev paged back to; null follows the newest. */
  revIndex: number | null;
  editsByRev: Readonly<Record<number, DraftEdits>>;
  notice: Notice | null;
  /** The rev our own Edit reserved, from its 202 until the steer list shows it. */
  waiting: Waiting | null;
  /** A reserved rev the card stopped waiting for. */
  timedOutRev: number | null;
  /** The rev our Retry reserved, until it lands, is declined, or the card stops waiting. */
  retryRev: number | null;
}

const FRESH: ItemState = {
  revIndex: null,
  editsByRev: {},
  notice: null,
  waiting: null,
  timedOutRev: null,
  retryRev: null,
};

type ItemPatch = Partial<ItemState> | ((current: ItemState) => Partial<ItemState>);

function patched(
  items: Readonly<Record<string, ItemState>>,
  itemId: string,
  patch: ItemPatch,
): Readonly<Record<string, ItemState>> {
  const before = items[itemId] ?? FRESH;
  return { ...items, [itemId]: { ...before, ...(typeof patch === "function" ? patch(before) : patch) } };
}

/** A steer whose rewrite has an outcome, or that ended without one. */
function settled(steer: SteerView): boolean {
  return steer.status !== "working" || steer.draft_outcome !== null;
}

/**
 * The rev an Edit reserved and is still writing, if any: ours from its 202 on, anyone's
 * from the steer list. And whether the newest reserved rev came to nothing.
 */
function rewriteOf(
  steers: readonly SteerView[],
  itemId: string,
  newestRev: number,
  { waiting, timedOutRev }: ItemState,
): { pending: number | null; unchanged: boolean } {
  const reserving = steers.filter(
    (steer) => steer.kind === "edit" && steer.escalation_id === itemId && steer.draft_rev !== null && steer.draft_rev > newestRev,
  );
  const ours = waiting === null ? undefined : steers.find((steer) => steer.id === waiting.steerId);
  const revs = reserving.filter((steer) => !settled(steer)).map((steer) => steer.draft_rev ?? 0);
  if (waiting !== null && waiting.rev > newestRev && (ours === undefined || !settled(ours))) revs.push(waiting.rev);
  const pending = revs.length === 0 ? null : Math.max(...revs);
  if (pending !== null && pending !== timedOutRev) return { pending, unchanged: false };
  const failed = reserving.some((steer) => settled(steer));
  return { pending: null, unchanged: failed || (timedOutRev !== null && timedOutRev > newestRev) };
}

function successToast(body: ResolveEscalation, escalation: EscalationView, us: OrgRef, them: OrgRef): string {
  const consult = escalation.kind === "consult";
  if (body.action === "approve") return consult ? `Released to ${representativeName(us.name)}` : `Sent to ${them.name}`;
  return consult ? `Declined. Claude will let ${them.name} know.` : "Declined. The message won't be sent.";
}

export function useDecision(
  escalation: EscalationView | null,
  consult: ConsultView | null,
  steers: readonly SteerView[],
  threadId: string,
  us: OrgRef,
  them: OrgRef,
): Decision | null {
  const itemId = escalation?.id ?? "";
  const resolve = useResolveEscalation({ threadId, escalationId: itemId });
  const retryCall = useRetryEscalation({ threadId, escalationId: itemId });
  const blocked = budgetLine(useBudgetState());

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
  const { pending, unchanged } = rewriteOf(steers, itemId, drafts.at(-1)?.draft_rev ?? 0, state);
  const rewriting = pending !== null;
  const busy = resolve.isPending || retryCall.isPending;
  const declined = escalation?.refusal ?? null;
  const { retryRev } = state;
  const retrying =
    retryRev !== null && (drafts.at(-1)?.draft_rev ?? 0) < retryRev && (declined?.draft_rev ?? 0) < retryRev;

  useEffect(() => {
    if (pending === null) return;
    const timer = window.setTimeout(() => {
      setItems((current) => patched(current, itemId, { timedOutRev: pending }));
    }, REDRAFT_TIMEOUT_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [pending, itemId]);

  useEffect(() => {
    if (!retrying) return;
    const timer = window.setTimeout(() => {
      setItems((current) => patched(current, itemId, { retryRev: null }));
    }, REDRAFT_TIMEOUT_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [retrying, itemId]);

  if (escalation === null) return null;

  const fail = (error: unknown) => {
    const problem = problemOf(error);
    if (problem?.code === "nothing_to_retry") {
      update({ notice: { tone: "info", text: "Claude is past this step now. Check the latest before trying again." } });
      return;
    }
    if (problem?.code === "escalation_not_open") {
      // `resolved_by` is set only when someone resolved it; a cancelled item comes back without it.
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

  const send = (body: ResolveEscalation, onDone?: () => void) => {
    update({ notice: null });
    resolve.mutate(body, {
      onSuccess: () => {
        toast(successToast(body, escalation, us, them));
        onDone?.();
      },
      onError: fail,
    });
  };

  return {
    escalation,
    consult,
    drafts,
    index,
    rev,
    showRev: (next) => {
      update({ revIndex: next === drafts.length - 1 ? null : next });
    },
    answer: shownAnswer(rev, edits),
    edited: isEdited(rev, edits),
    setAnswer: (value) => {
      const key = rev?.draft_rev ?? 0;
      update((current) => ({ editsByRev: { ...current.editsByRev, [key]: { answer: value } } }));
    },
    undoEdits: () => {
      const key = rev?.draft_rev ?? 0;
      update((current) => ({ editsByRev: { ...current.editsByRev, [key]: {} } }));
    },
    rewriting,
    unchanged,
    refusal: retrying ? null : declined,
    retrying,
    retry: () => {
      if (busy || retrying || blocked !== null || declined === null) return;
      // An answer Claude declined to draft has no draft to send; one it declined to check is checked as shown.
      const draftText = rev !== null && isEdited(rev, edits) ? shownAnswer(rev, edits).trim() : null;
      if (draftText === "") {
        update({ notice: { tone: "error", text: "Write the answer to check, or undo your edits." } });
        return;
      }
      update({ notice: null });
      retryCall.mutate(
        { draft_text: draftText },
        {
          onSuccess: ({ draft_rev }) => {
            update({ retryRev: draft_rev, revIndex: null });
          },
          onError: fail,
        },
      );
    },
    awaitRewrite: (reserved, steerId) => {
      update({ waiting: { rev: reserved, steerId }, revIndex: null });
    },
    busy,
    blocked,
    notice: state.notice,
    clearNotice: () => {
      update({ notice: null });
    },
    approve: () => {
      if (busy || rewriting || retrying || blocked !== null) return;
      const built = approveBody(escalation.kind, rev, edits);
      if (!built.ok) {
        update({ notice: { tone: "error", text: built.error } });
        return;
      }
      send(built.body);
    },
    decline: (reason, onDone) => {
      if (busy || blocked !== null) return;
      send(declineBody(reason ?? "", rev), onDone);
    },
  };
}
