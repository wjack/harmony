/**
 * Why an item is with whom, and Reassign, which moves the item and its thread access to
 * another teammate (REVISION_UNSTRUCTURED.md §6). The routing line under the thread title
 * says it (UI_REFERENCE §7); the escalation panel repeats it beside the decision and
 * anchors the picker there, so it's in view whenever there's something to reassign.
 */
import { Check } from "lucide-react";
import { Popover } from "radix-ui";
import type { ReactNode } from "react";

import { firstName, roleLabel } from "../shell/names";
import { toast } from "../shell/toasts";
import { Avatar, Spark, cx } from "../shell/ui";
import { problemOf, useReassignEscalation } from "./api";
import { routedSentence } from "./timeline";
import type { EscalationView, MemberView, OrgRef } from "./types";

/** "Routed to you by Claude: <reason>". */
export function RoutedText({ escalation, memberId }: { escalation: EscalationView; memberId: string | undefined }) {
  return <>{routedSentence(escalation, memberId, firstName)}</>;
}

interface RoutingLineProps {
  escalation: EscalationView | null;
  memberId: string | undefined;
  /** Who shared the thread with the acting member, and what they asked. */
  sharedBy: { name: string; question: string | null } | null;
  /** Opens the escalation panel's member picker. */
  onReassign: () => void;
}

/**
 * How the thread reached the acting member: routed to them, or shared. With neither
 * there is no routing to state, and the thread's status is a property (the rail, or the
 * strip below it), so the line is left out rather than repeating it.
 */
export function RoutingLine({ escalation, memberId, sharedBy, onReassign }: RoutingLineProps) {
  let text: ReactNode;
  if (escalation !== null) {
    text = <RoutedText escalation={escalation} memberId={memberId} />;
  } else if (sharedBy !== null) {
    text = (
      <>
        {firstName(sharedBy.name)} shared this with you
        {sharedBy.question !== null && sharedBy.question !== "" && <>: “{sharedBy.question}”</>}
      </>
    );
  } else {
    return null;
  }

  return (
    <p className="text-[13.5px] leading-[1.55] text-muted">
      <Spark size={13} className="mr-1.5 inline-block align-[-2px]" />
      {text}
      {escalation !== null && (
        <>
          <span aria-hidden> · </span>
          <button type="button" onClick={onReassign} className={LINK}>
            Reassign
          </button>
        </>
      )}
    </p>
  );
}

const LINK = "font-medium text-ink underline decoration-border-strong underline-offset-[3px] hover:decoration-ink";

interface ReassignMenuProps {
  escalation: EscalationView;
  threadId: string;
  memberId: string | undefined;
  us: OrgRef;
  members: readonly MemberView[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** The "Reassign" link and its member picker. */
export function ReassignMenu({ open, onOpenChange, ...picker }: ReassignMenuProps) {
  return (
    <Popover.Root open={open} onOpenChange={onOpenChange}>
      <Popover.Trigger asChild>
        <button type="button" className={LINK}>
          Reassign
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        {/* The trigger sits at the panel's right edge, so the picker opens leftward. */}
        <Popover.Content
          align="end"
          side="top"
          sideOffset={8}
          collisionPadding={12}
          className="z-50 w-[320px] rounded-menu border border-border bg-surface p-1.5 text-ink shadow-menu outline-none data-[state=open]:animate-pop-in"
        >
          <ReassignPicker
            {...picker}
            onDone={() => {
              onOpenChange(false);
            }}
          />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

interface ReassignPickerProps {
  escalation: EscalationView;
  threadId: string;
  memberId: string | undefined;
  us: OrgRef;
  members: readonly MemberView[];
  onDone: () => void;
}

/** One click moves the item: there's nothing to explain, since routing doesn't learn from it. */
function ReassignPicker({ escalation, threadId, memberId, us, members, onDone }: ReassignPickerProps) {
  const reassign = useReassignEscalation({ threadId, escalationId: escalation.id });
  const assigned = new Set(escalation.assignees.map((assignee) => assignee.id));
  const pendingId = reassign.isPending ? reassign.variables.member_id : null;
  const error = reassign.error === null ? null : (problemOf(reassign.error)?.detail ?? reassign.error.message);

  const pick = (member: MemberView) => {
    reassign.mutate(
      { member_id: member.id },
      {
        onSuccess: () => {
          toast(`Reassigned to ${firstName(member.name)}`);
          onDone();
        },
      },
    );
  };

  return (
    <div role="listbox" aria-label="Reassign to" aria-busy={reassign.isPending}>
      <p className="px-2 pt-1 pb-1.5 text-[12.5px] text-muted">Reassign to</p>
      {members.length === 0 && <p className="px-2 py-2 text-[13px] text-muted">Loading teammates…</p>}
      {members.map((member) => {
        const current = assigned.has(member.id);
        return (
          <button
            key={member.id}
            type="button"
            role="option"
            aria-selected={current}
            disabled={current || reassign.isPending}
            onClick={() => {
              pick(member);
            }}
            className={cx(
              "flex w-full items-center gap-2.5 rounded-control px-2 py-1.5 text-left",
              current ? "cursor-default" : "enabled:hover:bg-hover",
            )}
          >
            <Avatar name={member.name} seed={us.slug} size={26} />
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate text-[13.5px] leading-5">
                {member.name}
                {member.id === memberId && <span className="text-muted"> (you)</span>}
              </span>
              <span className="truncate text-[12px] leading-4 text-muted">{member.title ?? roleLabel(member.role)}</span>
            </span>
            {current && <Check size={15} strokeWidth={1.75} aria-label="Assigned" className="shrink-0 text-ink" />}
            {pendingId === member.id && <Spark size={14} animated label="Reassigning" />}
          </button>
        );
      })}
      {error !== null && (
        <p role="alert" className="px-2 pt-1.5 pb-1 text-[12.5px] text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
