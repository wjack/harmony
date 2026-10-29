/**
 * The card for a turn Claude declined (`refused_turn`, EscalationView.refusal step
 * `turn`): the conversation waits for our side instead of closing. A note says so, with
 * the category when Claude gave one, and three ways on. Retry runs Claude's turn again.
 * Edit opens the box's Edit: Claude tries again once the instruction is sent, and opens
 * with it. End the conversation closes it, as the refusal used to; Reopen still works.
 */
import { CircleHelp, Pencil, RotateCcw } from "lucide-react";

import { CardHint, FootShell } from "../cards";
import { Button } from "../shell/ui";
import { eyebrow, refusalLine } from "./decision";
import { ReassignMenu } from "./ReassignMenu";
import type { MemberView, OrgRef } from "./types";
import type { DeclinedTurn } from "./useDeclinedTurn";

interface DeclinedTurnCardProps {
  turn: DeclinedTurn;
  threadId: string;
  them: OrgRef;
  us: OrgRef;
  /** Our org's members, for Reassign. */
  members: readonly MemberView[];
  memberId: string | undefined;
  /** Opens the box's Edit for an instruction Claude retries with. */
  onEdit: () => void;
  /** The box holds an instruction to Claude that isn't sent yet. */
  unsentEdit: boolean;
}

export function DeclinedTurnCard({ turn, threadId, them, us, members, memberId, onEdit, unsentEdit }: DeclinedTurnCardProps) {
  const { escalation, busy, blocked, notice } = turn;
  const note = refusalLine(escalation.refusal ?? { step: "turn", category: null, draft_rev: null });
  return (
    <section
      aria-label="Needs your decision"
      className="relative rounded-composer border border-border-strong bg-surface shadow-card"
    >
      <div className="flex flex-col gap-1.5 px-4 pt-3.5 pb-4">
        <div className="flex items-start gap-3">
          <p className="flex min-w-0 flex-1 items-start gap-1.5 text-ui-sm text-muted">
            <CircleHelp size={14} strokeWidth={1.75} aria-hidden className="mt-0.5 shrink-0" />
            <span>{eyebrow(escalation, them, memberId)}</span>
          </p>
          <ReassignMenu escalation={escalation} threadId={threadId} memberId={memberId} us={us} members={members} />
        </div>
        <h2 className="text-ui-title font-medium text-ink">{note}</h2>
        <CardHint>
          {unsentEdit
            ? "Claude tries again with your instruction when you send it."
            : `Retry runs Claude's turn again. To change what it does first, tell it with Edit.`}
        </CardHint>
        {notice !== null && (
          <p role="alert" className="mt-1 rounded-control bg-danger-soft px-3 py-2 text-ui-sm text-danger">
            {notice}
          </p>
        )}
      </div>
      <FootShell>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" disabled={busy} onClick={turn.end}>
            End the conversation
          </Button>
          <span className="flex-1" />
          <Button icon={<Pencil size={14} strokeWidth={1.75} />} disabled={busy} onClick={onEdit}>
            Edit
          </Button>
          <Button
            variant="primary"
            icon={<RotateCcw size={14} strokeWidth={1.75} />}
            disabled={busy || blocked !== null}
            title={blocked ?? undefined}
            onClick={turn.retry}
          >
            Retry
          </Button>
        </div>
        {blocked !== null && <p className="mt-2 text-right text-ui-xs text-muted">{blocked}</p>}
      </FootShell>
    </section>
  );
}
