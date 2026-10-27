/**
 * Reassign (REVISION_UNSTRUCTURED.md §6): moves an open item, and its thread access, to
 * another teammate. It sits in the decision card's "Why it came to you", beside the
 * routing it overrides.
 */
import { useQueryClient } from "@tanstack/react-query";
import { Check } from "lucide-react";
import { Popover } from "radix-ui";
import { useState } from "react";
import { useNavigate } from "react-router";

import { firstName, roleLabel } from "../shell/names";
import { toast } from "../shell/toasts";
import { Avatar, Spark, cx } from "../shell/ui";
import { lostThread, problemOf, useReassignEscalation } from "./api";
import type { EscalationView, MemberView, OrgRef } from "./types";

const LINK = "font-medium text-ink underline decoration-border-strong underline-offset-[3px] hover:decoration-ink";

interface ReassignMenuProps {
  escalation: EscalationView;
  threadId: string;
  memberId: string | undefined;
  us: OrgRef;
  members: readonly MemberView[];
}

/** The "Reassign" link and its member picker. */
export function ReassignMenu(picker: ReassignMenuProps) {
  const [open, setOpen] = useState(false);
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button type="button" className={LINK}>
          Reassign
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          side="bottom"
          sideOffset={8}
          collisionPadding={12}
          className="z-50 w-[320px] rounded-menu border border-border bg-surface p-1.5 text-ink shadow-menu outline-none data-[state=open]:animate-pop-in"
        >
          <ReassignPicker
            {...picker}
            onDone={() => {
              setOpen(false);
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

/**
 * One click moves the item: there's nothing to explain, since routing doesn't learn from
 * it. Moving it can take away the viewer's only access (a member it was routed to), and
 * then the view has nothing left to show them: they land on Requests.
 */
function ReassignPicker({ escalation, threadId, memberId, us, members, onDone }: ReassignPickerProps) {
  const reassign = useReassignEscalation({ threadId, escalationId: escalation.id });
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const assigned = new Set(escalation.assignees.map((assignee) => assignee.id));
  const pendingId = reassign.isPending ? reassign.variables.member_id : null;
  const error = reassign.error === null ? null : (problemOf(reassign.error)?.detail ?? reassign.error.message);

  const pick = (member: MemberView) => {
    // Awaited, not a `mutate` callback: losing the thread unmounts this picker before
    // the refetch that says so settles.
    reassign.mutateAsync({ member_id: member.id }).then(
      () => {
        const name = firstName(member.name);
        if (lostThread(queryClient, threadId)) {
          toast(`Reassigned to ${name}. It's no longer in your Requests.`);
          void navigate("/requests");
          return;
        }
        toast(`Reassigned to ${name}`);
        onDone();
      },
      // The picker shows the failure.
      () => undefined,
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
