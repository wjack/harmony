/**
 * "What our representative saw" (REVISION_UNSTRUCTURED.md §2): exactly the parts of
 * Claude for <us>'s frozen prompt on this thread, the notices Harmony sent it since (a
 * changed setting, mode or sharing description replaces what its prompt says, R§12), and
 * each result its side gave it: a settled consult's, and a held message's when it quotes
 * what a person wrote. The server lists a notice or result only once a stored call
 * carried it. Nothing else from our side ever reaches the representative.
 */
import { Bell, Building2, FileText, IdCard, MessageCircleQuestion, ScrollText, Share2, X } from "lucide-react";
import { Dialog } from "radix-ui";
import { useState, type ReactNode } from "react";

import { useCalendar } from "../demo/worldClock";
import { firstName } from "../shell/names";
import { cx } from "../shell/ui";
import { dateTime, representativeName } from "./format";
import type { ContextPart, DeliveredNotice, OrgRef, ReleasedAnswer, RepresentativeContext } from "./types";

const PART_ICON: Record<ContextPart["kind"], ReactNode> = {
  public_summary: <IdCard size={15} strokeWidth={1.5} />,
  instructions: <ScrollText size={15} strokeWidth={1.5} />,
  sharing: <Share2 size={15} strokeWidth={1.5} />,
  brief: <FileText size={15} strokeWidth={1.5} />,
  counterparty: <Building2 size={15} strokeWidth={1.5} />,
};

// Past this, a row shows its first lines until opened.
const LONG_TEXT = 280;

interface RowProps {
  icon: ReactNode;
  heading: string;
  text: string;
  /** Who and when, under the text. */
  meta?: string;
}

function Row({ icon, heading, text, meta }: RowProps) {
  const long = text.length > LONG_TEXT;
  const [open, setOpen] = useState(false);
  return (
    <li className="flex gap-3 px-3.5 py-3">
      <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-control bg-hover text-secondary">
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <h4 className="text-[13.5px] leading-5 font-medium text-ink">{heading}</h4>
        <p
          className={cx(
            "mt-0.5 text-[12.5px] leading-[18px] whitespace-pre-wrap text-secondary",
            long && !open && "line-clamp-4",
          )}
        >
          {text === "" ? <span className="text-muted">Empty</span> : text}
        </p>
        {long && (
          <button
            type="button"
            aria-expanded={open}
            onClick={() => {
              setOpen((value) => !value);
            }}
            className="mt-1 text-[12.5px] font-medium text-secondary underline decoration-border-strong underline-offset-2 hover:text-ink"
          >
            {open ? "Show less" : "Show all"}
          </button>
        )}
        {meta !== undefined && <span className="mt-1 block text-[12px] leading-[18px] text-muted">{meta}</span>}
      </div>
    </li>
  );
}

const NOTICE_HEADING: Record<DeliveredNotice["kind"], string> = {
  settings: "Your settings changed",
  mode: "How answers are reviewed changed",
  sharing: "What it may pass on changed",
  world_date: "Today's date",
  closed: "The other side closed the conversation",
};

function releasedBy(answer: ReleasedAnswer): string {
  if (answer.released_by === "auto" || answer.released_by_member === null) return "Released by Claude";
  return `Released by ${firstName(answer.released_by_member.name)}`;
}

// A held message's result answers no question of the representative's own.
const HELD_MESSAGE_HEADING = "About a message your side held";

function Listed({ label, count, children }: { label: string; count: number; children: ReactNode }) {
  return (
    <section aria-label={label}>
      <h3 className="mb-2 text-[12.5px] text-muted">
        {label} · {count}
      </h3>
      {count === 0 ? (
        <p className="text-[12.5px] text-muted">None yet.</p>
      ) : (
        <ul className="divide-y divide-border overflow-hidden rounded-menu border border-border bg-surface">{children}</ul>
      )}
    </section>
  );
}

interface RepresentativeSawProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  us: OrgRef;
  them: OrgRef;
  representative: RepresentativeContext | null;
}

export function RepresentativeSaw({ open, onOpenChange, us, them, representative }: RepresentativeSawProps) {
  const calendar = useCalendar();
  const released = [...(representative?.released ?? [])].sort(
    (a, b) => Date.parse(a.released_at) - Date.parse(b.released_at),
  );

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-[rgb(20_20_19/0.16)] data-[state=open]:animate-fade-in" />
        <Dialog.Content className="fixed inset-y-2 right-2 z-50 flex w-[min(440px,calc(100vw-16px))] flex-col rounded-2xl border border-border bg-canvas text-ink shadow-modal outline-none data-[state=open]:animate-pop-in">
          <header className="flex items-start gap-3 border-b border-border px-5 pt-4 pb-3.5">
            <div className="min-w-0 flex-1">
              <Dialog.Title className="text-[16px] leading-6 font-semibold">What our representative saw</Dialog.Title>
              <Dialog.Description className="mt-0.5 text-[13px] leading-5 text-muted">
                Everything {representativeName(us.name)} had on this thread with {them.name}: its prompt, and the
                notices and answers your side gave it since.
              </Dialog.Description>
            </div>
            <Dialog.Close
              aria-label="Close"
              className="-mt-0.5 -mr-1.5 inline-flex size-8 shrink-0 items-center justify-center rounded-control text-muted transition-colors hover:bg-hover hover:text-ink"
            >
              <X size={18} strokeWidth={1.5} />
            </Dialog.Close>
          </header>

          <div className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto px-5 py-4">
            {representative === null ? (
              <p className="py-8 text-center text-[13px] text-muted">
                Nothing yet. Its prompt is set at its first turn on this thread.
              </p>
            ) : (
              <div className="flex flex-col gap-5">
                <section aria-label="Its prompt">
                  <h3 className="mb-2 text-[12.5px] text-muted">
                    Its prompt · set {dateTime(representative.frozen_at, calendar)}
                  </h3>
                  <ul className="divide-y divide-border overflow-hidden rounded-menu border border-border bg-surface">
                    {representative.parts.map((part) => (
                      <Row
                        key={`${part.kind}:${part.label}`}
                        icon={PART_ICON[part.kind]}
                        heading={part.label}
                        text={part.text}
                      />
                    ))}
                  </ul>
                </section>
                <Listed label="Notices since" count={representative.notices.length}>
                  {/* A notice has no id; the list is append-only, in the order sent. */}
                  {representative.notices.map((notice, index) => (
                    <Row
                      key={index}
                      icon={<Bell size={15} strokeWidth={1.5} />}
                      heading={NOTICE_HEADING[notice.kind]}
                      text={notice.text}
                      meta={`Sent ${dateTime(notice.delivered_at, calendar)}`}
                    />
                  ))}
                </Listed>
                <Listed label="Released answers" count={released.length}>
                  {released.map((answer) => (
                    <Row
                      key={answer.id}
                      icon={<MessageCircleQuestion size={15} strokeWidth={1.5} />}
                      heading={answer.question ?? HELD_MESSAGE_HEADING}
                      text={answer.text}
                      meta={`${releasedBy(answer)} · ${dateTime(answer.released_at, calendar)}`}
                    />
                  ))}
                </Listed>
              </div>
            )}
          </div>

          <footer className="border-t border-border px-5 py-3 text-[12px] leading-[18px] text-muted">
            The representative never reads your files. Besides what {them.name} wrote on this thread, everything it
            knows is listed here. Once something is shared, {them.name} holds it.
          </footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
