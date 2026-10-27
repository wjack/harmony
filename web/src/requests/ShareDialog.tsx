/**
 * Share a thread with a teammate (ARCHITECTURE §10), in the shape of Claude's "Share
 * chat" modal: a bordered list to pick from, an optional question, and a black primary.
 */
import { Check } from "lucide-react";
import { useState } from "react";

import { firstName, roleLabel } from "../shell/names";
import { toast } from "../shell/toasts";
import { Avatar, Button, Modal, ModalList, cx } from "../shell/ui";
import { useShareThread } from "./api";
import type { MemberView, OrgRef, ThreadAccess } from "./types";

interface ShareDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  threadId: string;
  us: OrgRef;
  members: readonly MemberView[];
  access: readonly ThreadAccess[];
  memberId: string | undefined;
  /** Focuses what opened it: it has no Radix trigger, so focus would fall to the page. */
  returnFocus: () => void;
}

export function ShareDialog({
  open,
  onOpenChange,
  threadId,
  us,
  members,
  access,
  memberId,
  returnFocus,
}: ShareDialogProps) {
  const share = useShareThread(threadId);
  const [picked, setPicked] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const withAccess = new Set(access.map((row) => row.member_id));
  const teammates = members.filter((member) => member.id !== memberId);
  const pickedMember = teammates.find((member) => member.id === picked) ?? null;

  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) {
      setPicked(null);
      setQuestion("");
      share.reset();
    }
  };

  return (
    <Modal
      open={open}
      onOpenChange={close}
      onCloseAutoFocus={(event) => {
        event.preventDefault();
        returnFocus();
      }}
      title="Share with a teammate"
      description={`They'll see this conversation, Claude's drafts and your team's comments. Only people at ${us.name} can be added.`}
      footer={
        <>
          <Button
            variant="secondary"
            onClick={() => {
              close(false);
            }}
          >
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={pickedMember === null || share.isPending}
            onClick={() => {
              if (pickedMember === null) return;
              share.mutate(
                { member_id: pickedMember.id, question: question.trim() === "" ? null : question.trim() },
                {
                  onSuccess: () => {
                    toast(`Shared with ${firstName(pickedMember.name)}`);
                    close(false);
                  },
                },
              );
            }}
          >
            Share
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <ModalList className="bg-surface">
          {teammates.length === 0 && <p className="px-4 py-3 text-sm text-muted">No teammates to share with.</p>}
          {teammates.map((member) => {
            const has = withAccess.has(member.id);
            const selected = member.id === picked;
            return (
              <button
                key={member.id}
                type="button"
                disabled={has}
                aria-pressed={selected}
                onClick={() => {
                  setPicked(member.id);
                }}
                className={cx(
                  "flex w-full items-center gap-3 px-4 py-3 text-left transition-colors",
                  has ? "cursor-default" : "hover:bg-hover/60",
                )}
              >
                <Avatar name={member.name} seed={us.slug} size={30} />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className={cx("truncate text-[14px] leading-5 font-medium", has && "text-muted")}>
                    {member.name}
                  </span>
                  <span className="truncate text-[13px] leading-5 text-muted">
                    {has ? "Already has access" : (member.title ?? roleLabel(member.role))}
                  </span>
                </span>
                {selected && <Check size={18} strokeWidth={1.75} aria-hidden className="shrink-0 text-toggle" />}
              </button>
            );
          })}
        </ModalList>
        <label className="flex flex-col gap-1.5">
          <span className="text-[13px] font-medium text-ink">
            Ask {pickedMember === null ? "them" : firstName(pickedMember.name)} something{" "}
            <span className="font-normal text-muted">(optional)</span>
          </span>
          <textarea
            value={question}
            onChange={(event) => {
              setQuestion(event.target.value);
            }}
            rows={3}
            placeholder="e.g. Can we run the reserve XRD slot on Nov 4?"
            className="w-full resize-none rounded-control border border-border-strong bg-surface px-3 py-2 text-[14px] leading-5 outline-none placeholder:text-faint focus:border-muted"
          />
        </label>
        {share.error !== null && (
          <p role="alert" className="text-[13px] text-danger">
            {share.error.message}
          </p>
        )}
      </div>
    </Modal>
  );
}
