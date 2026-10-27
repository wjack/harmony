/**
 * What the member has chosen on an outreach card before sending it: who gets the
 * request, and their own wording of the brief and of each organization's ask. The chat
 * keeps it per card, so putting the card away and bringing it back keeps the choices,
 * and the composer's "Review" strip can count the ticked set (LIVE_DEMO_PLAN 3.13 E4).
 */
import { editedText } from "./brief";
import { plural } from "./format";
import { outreachRecipients } from "./recipients";
import type { ApproveCard, CardRecipient, OutreachCardItem } from "./types";

export interface OutreachDraft {
  /** The ticked organizations, by org id. */
  chosen: ReadonlySet<string>;
  brief: string;
  /** Asks the member has typed into, by org id; the others read as Claude wrote them. */
  asks: Readonly<Record<string, string>>;
}

export function initialDraft(card: OutreachCardItem): OutreachDraft {
  return { chosen: outreachRecipients(card).proposed, brief: card.brief, asks: {} };
}

/** The ask as the member sees it now: their edit, else Claude's. */
export function askText(draft: OutreachDraft, recipient: CardRecipient): string {
  return draft.asks[recipient.org.id] ?? recipient.ask ?? "";
}

/**
 * The approve body. It always names the recipients, in the card's order, so the
 * server's default (the suggested rows) never stands in for what the member ticked; and
 * it carries only the wording that changed, for the organizations it goes to.
 */
export function approveBody(card: OutreachCardItem, draft: OutreachDraft): ApproveCard {
  const recipients = card.recipients.filter((recipient) => draft.chosen.has(recipient.org.id));
  const brief = editedText(card.brief, draft.brief);
  const asks = Object.fromEntries(
    recipients.flatMap((recipient) => {
      const ask = editedText(recipient.ask ?? "", askText(draft, recipient));
      return ask === null ? [] : [[recipient.org.id, ask]];
    }),
  );
  return {
    recipients: recipients.map((recipient) => recipient.org.id),
    ...(brief === null ? {} : { brief }),
    ...(Object.keys(asks).length === 0 ? {} : { asks }),
  };
}

/** Whether the member reworded anything that would go out. */
export function hasEdits(card: OutreachCardItem, draft: OutreachDraft): boolean {
  const body = approveBody(card, draft);
  return body.brief !== undefined || body.asks !== undefined;
}

/** The card's question, and the composer strip that brings it back: the ticked set's size. */
export function outreachTitle(draft: OutreachDraft): string {
  return `Send this request to ${plural(draft.chosen.size, "organization")}?`;
}
