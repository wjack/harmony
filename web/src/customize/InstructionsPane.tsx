import { Info } from "lucide-react";

import { PROSE_MAX } from "./draft";
import { Callout, CharCount, Section, TextArea } from "./fields";
import type { PaneProps } from "./pane";

/** Instructions: the representative's private prompt, in prose (§9). */
export function InstructionsPane({ draft, edit, editable, onSection }: PaneProps) {
  return (
    <Section
      title="Instructions"
      description="Only your representative reads these. Counterparties never see them."
      aside={<CharCount value={draft.instructions} max={PROSE_MAX} />}
    >
      <TextArea
        aria-label="Instructions"
        rows={16}
        value={draft.instructions}
        disabled={!editable}
        onChange={(event) => {
          edit({ instructions: event.target.value });
        }}
        placeholder="Prioritize academic groups with funding deadlines. Keep a friendly, concise tone. Check with the team before promising anything outside the public calendar."
        className="min-h-[280px]"
      />
      <Callout icon={<Info size={15} strokeWidth={1.75} />} className="mt-4">
        Your representative doesn&rsquo;t see your files. When it needs a price or a date, it asks internal Claude, which
        looks it up and drafts the answer with{" "}
        <button
          type="button"
          onClick={() => {
            onSection("sharing");
          }}
          className="font-medium text-ink underline decoration-border-strong underline-offset-2 hover:decoration-ink"
        >
          what you share
        </button>{" "}
        in mind.
      </Callout>
    </Section>
  );
}
