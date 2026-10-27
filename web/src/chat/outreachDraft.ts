/**
 * What the member has chosen on an outreach card before sending it: who gets the
 * request, their own wording of the brief and of each organization's ask, and which of
 * Claude's wordings each rewritten part shows. The chat keeps it per card, so a refetch
 * or folding the card keeps the choices, and the "Review" strip can count the ticked set.
 */
import { editedText } from "./brief";
import { plural } from "./format";
import { wasReplaced } from "./items";
import { outreachRecipients } from "./recipients";
import type { ApproveCard, CardRecipient, ChatItem, OutreachCardItem } from "./types";

export interface OutreachDraft {
  /** The ticked organizations, by org id. */
  chosen: ReadonlySet<string>;
  brief: string;
  /** Asks the member has typed into or paged back, by org id; the others read as Claude wrote them. */
  asks: Readonly<Record<string, string>>;
  /** Which of Claude's wordings a rewritten part shows, by part ("brief" or an org id); the latest when absent. */
  shown: Readonly<Record<string, number>>;
}

export const BRIEF_PART = "brief";
/** The longest brief or ask the server takes (`BRIEF_MAX_CHARS`). */
export const PART_MAX_CHARS = 8000;

export function initialDraft(card: OutreachCardItem): OutreachDraft {
  return { chosen: outreachRecipients(card).proposed, brief: card.brief, asks: {}, shown: {} };
}

/** The ask as the member sees it now: their edit, else Claude's. */
export function askText(draft: OutreachDraft, recipient: CardRecipient): string {
  return draft.asks[recipient.org.id] ?? recipient.ask ?? "";
}

/**
 * The approve body. It always names the recipients, in the card's order, so the
 * server's default (the suggested rows) never stands in for what the member ticked; and
 * it carries only the wording that differs from the card's, for the organizations it
 * goes to. An earlier wording paged back to goes as an edit.
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

/** Whether anything that would go out reads differently from the card. */
export function hasEdits(card: OutreachCardItem, draft: OutreachDraft): boolean {
  return carriesEdits(approveBody(card, draft));
}

/** Whether an approve body sends wording of the member's own. */
export function carriesEdits(body: ApproveCard): boolean {
  return body.brief !== undefined || body.asks !== undefined;
}

/**
 * Whether the member cleared a part Claude had written: the brief, or a ticked
 * organization's ask. An empty part would go out as Claude wrote it, which they didn't
 * ask for, so it holds the send.
 */
export function hasClearedPart(card: OutreachCardItem, draft: OutreachDraft): boolean {
  if (draft.brief.trim() === "") return true;
  return card.recipients.some(
    (recipient) =>
      draft.chosen.has(recipient.org.id) && (recipient.ask ?? "").trim() !== "" && askText(draft, recipient).trim() === "",
  );
}

/** The "Review" strip's question: the ticked set's size. */
export function outreachTitle(draft: OutreachDraft): string {
  return `Send this request to ${plural(draft.chosen.size, "organization")}?`;
}

/** Claude's wordings of each part, oldest first, ending with the card's own. */
export interface PartVersions {
  brief: readonly string[];
  asks: Readonly<Record<string, readonly string[]>>;
}

/**
 * Each part's wordings across the drafts this card replaced: the cards before it that
 * gave way to a newer draft, back to the last one that didn't. A part that reads the
 * same in two drafts in a row counts once, so only what Claude rewrote has versions.
 */
export function partVersions(items: readonly ChatItem[], card: OutreachCardItem): PartVersions {
  const cards = items.filter((item): item is OutreachCardItem => item.type === "outreach_card");
  const at = cards.findIndex((each) => each.card_id === card.card_id);
  const chain = [card];
  for (let index = at - 1; index >= 0; index -= 1) {
    const earlier = cards[index];
    if (earlier === undefined || !wasReplaced(earlier)) break;
    chain.unshift(earlier);
  }
  const asks = Object.fromEntries(
    card.recipients.map((recipient) => [
      recipient.org.id,
      distinct(
        chain.flatMap((each) => {
          const ask = each.recipients.find((row) => row.org.id === recipient.org.id)?.ask;
          return ask === undefined || ask === null ? [] : [ask];
        }),
      ),
    ]),
  );
  return { brief: distinct(chain.map((each) => each.brief)), asks };
}

function distinct(texts: readonly string[]): string[] {
  return texts.filter((text, index) => index === 0 || text.trim() !== texts[index - 1]?.trim());
}

/** Shows Claude's `index`th wording of `part`, in place of whatever the member had there. */
export function showVersion(draft: OutreachDraft, versions: PartVersions, part: string, index: number): OutreachDraft {
  const shown = { ...draft.shown, [part]: index };
  if (part === BRIEF_PART) return { ...draft, shown, brief: versions.brief[index] ?? draft.brief };
  const text = versions.asks[part]?.[index];
  return text === undefined ? draft : { ...draft, shown, asks: { ...draft.asks, [part]: text } };
}

/** Claude's wording a part shows now: the one paged to, else the latest. */
export function shownVersion(versions: readonly string[], draft: OutreachDraft, part: string): { index: number; text: string } {
  const index = draft.shown[part] ?? versions.length - 1;
  return { index, text: versions[index] ?? "" };
}
