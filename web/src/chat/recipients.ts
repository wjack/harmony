/** Who the outreach card sends to unless the member chooses otherwise (R§10.1, LIVE_DEMO_PLAN 3.13 E4). */
import type { CardRecipient, OutreachCardItem } from "./types";

export interface OutreachRecipients {
  /** Every organization Claude listed. */
  listed: ReadonlySet<string>;
  /** Those the card ticks, and sends to, by default. */
  proposed: ReadonlySet<string>;
}

/**
 * Claude marks the organizations it suggests; one it lists only for the member's
 * decision (a near miss) starts unticked. A card from before suggestions falls back to
 * the screening: everything but a "not a fit". The server's `default_recipients`
 * follows the same rule.
 */
function defaultRecipients(rows: readonly CardRecipient[]): CardRecipient[] {
  const suggested = rows.filter((row) => row.suggested === true);
  if (suggested.length > 0) return suggested;
  const fitting = rows.filter((row) => row.fit !== "no");
  return fitting.length > 0 ? fitting : [...rows];
}

export function outreachRecipients(card: OutreachCardItem): OutreachRecipients {
  return {
    listed: new Set(card.recipients.map((recipient) => recipient.org.id)),
    proposed: new Set(defaultRecipients(card.recipients).map((recipient) => recipient.org.id)),
  };
}
