/** The shared card parts (/__gallery), in a card shaped like the request view's. */
import { useRef, useState } from "react";

import type { GallerySection } from "../gallery/types";
import { CardFoot, CardHint, DeclineStep, EditableText, PartLabel, useApproveShortcut } from ".";

const DRAFTS = [
  "Thanks for the follow-up questions. SEM/EDS can run on November 4 to 6, and XRD opens on November 10.",
  "Thanks for the follow-up questions. SEM/EDS can run on November 4 to 6. XRD opens on November 10 and results are ready on November 12.",
];

function Card({ required }: { required: boolean }) {
  const card = useRef<HTMLElement>(null);
  const [index, setIndex] = useState(DRAFTS.length - 1);
  const [text, setText] = useState(DRAFTS[index] ?? "");
  const [declining, setDeclining] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const original = DRAFTS[index] ?? "";
  const show = (next: number) => {
    setIndex(next);
    setText(DRAFTS[next] ?? "");
  };
  useApproveShortcut(card, () => { setDone("Approved"); }, !declining);
  return (
    <section ref={card} className="max-w-[640px] rounded-composer border border-border-strong bg-surface shadow-card">
      <div className="flex flex-col gap-2 px-4 pt-4">
        <h3 className="text-ui-title font-medium text-ink">Can SEM/EDS stay air-free, and can all 14 finish by Nov 11?</h3>
        <CardHint>Claude wrote this before your side's latest edit reached it.</CardHint>
        <div className="mt-2 border-t border-border pt-3">
          <PartLabel
            label="Draft reply"
            rewritten={{
              index,
              count: DRAFTS.length,
              onPrev: () => { show(index - 1); },
              onNext: () => { show(index + 1); },
            }}
            edited={text !== original ? { onUndo: () => { setText(original); } } : undefined}
          />
          <div className="mt-1.5">
            <EditableText value={text} original={original} onChange={setText} label="Draft reply" />
          </div>
        </div>
        {done !== null && <p className="text-ui-sm text-muted">{done}</p>}
      </div>
      {declining ? (
        <DeclineStep
          required={required}
          title={required ? "Decline to answer" : "Don't send this request"}
          caption={required ? "Claude may pass this reason on to Henderson Lab." : "Only Claude in this chat sees this."}
          onCancel={() => { setDeclining(false); }}
          onConfirm={(reason) => {
            setDeclining(false);
            setDone(reason === null ? "Declined" : `Declined: ${reason}`);
          }}
        />
      ) : (
        <CardFoot
          onDecline={() => { setDeclining(true); }}
          onApprove={() => { setDone("Approved"); }}
          hintRight={<>Claude replies to Henderson Lab with this answer · ⌘↵</>}
        />
      )}
    </section>
  );
}

export default [
  {
    title: "Card parts",
    description:
      "EditableText, PartLabel (rewritten, edited, both), CardHint and the sticky CardFoot. Click the draft to change it; Decline opens the required step.",
    render: () => <Card required />,
  },
  {
    title: "Card parts, optional decline",
    description: "The outreach card's Decline takes no reason.",
    render: () => <Card required={false} />,
  },
] satisfies GallerySection[];
