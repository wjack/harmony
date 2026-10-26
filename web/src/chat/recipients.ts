/** Who the outreach card sends to unless the member chooses otherwise (R§10.1). */
import type { OutreachCardItem } from "./types";

export interface OutreachRecipients {
  /** Every organization Claude listed. */
  listed: ReadonlySet<string>;
  /** Those the card sends to by default. */
  proposed: ReadonlySet<string>;
}

/**
 * Claude may list an organization the screening found no fit so the member can choose
 * it: it starts unticked, and the card's first option sends to the others.
 */
export function outreachRecipients(card: OutreachCardItem): OutreachRecipients {
  const listed = card.recipients.filter((recipient) => recipient.selected);
  const fitting = listed.filter((recipient) => recipient.fit !== "no");
  return {
    listed: new Set(listed.map((recipient) => recipient.org.id)),
    proposed: new Set((fitting.length > 0 ? fitting : listed).map((recipient) => recipient.org.id)),
  };
}
