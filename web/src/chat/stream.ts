/**
 * Streaming a conversation turn's text from `stream.chat_delta` events.
 *
 * Deltas aren't persisted and carry no step index, and each model response is stored
 * (and refetched as `assistant_text` items) while later steps keep streaming. So the
 * buffer holds everything received for the turn's current attempt, and what shows is
 * the part after the text the turn has committed since the buffer began.
 *
 * The buffer can begin partway through a step (the chat opened mid-turn), and the
 * items can lag behind it (a step committed just before the buffer began, not yet
 * refetched). So the newly committed text is matched against the buffer instead of
 * counted off its start: the longest stretch at the start of the buffer that ends the
 * newly committed text is the part already stored. Whitespace is ignored, because
 * stored blocks are trimmed.
 *
 * A higher `attempt` means the activity was retried (or resumed after a card), so the
 * buffer restarts and late frames from earlier attempts are dropped (§8: a retried
 * attempt's partial stream is discarded). A new turn retires the old one the same way.
 */

export interface TurnStream {
  turnId: string;
  attempt: number;
  /** Everything received for this attempt, in order. */
  received: string;
  /** The turn's committed text items when this attempt's buffer began. */
  seen: readonly string[];
  /** Turns this buffer has moved past, most recent first; their late frames are dropped. */
  past: readonly string[];
}

export interface DeltaLike {
  turn_id: string;
  attempt: number;
  text: string;
}

/** One committed `assistant_text` block of the turn. */
export interface CommittedStep {
  id: string;
  markdown: string;
}

const PAST_TURNS_KEPT = 16;

export function applyDelta(
  state: TurnStream | null,
  delta: DeltaLike,
  committed: readonly CommittedStep[],
): TurnStream {
  const restart = (past: readonly string[]): TurnStream => ({
    turnId: delta.turn_id,
    attempt: delta.attempt,
    received: delta.text,
    seen: committed.map((step) => step.id),
    past,
  });
  if (state === null) return restart([]);
  if (state.past.includes(delta.turn_id)) return state;
  if (delta.turn_id !== state.turnId) return restart([state.turnId, ...state.past].slice(0, PAST_TURNS_KEPT));
  if (delta.attempt < state.attempt) return state;
  if (delta.attempt > state.attempt) return restart(state.past);
  return { ...state, received: state.received + delta.text };
}

/** The part of the stream not yet committed as `assistant_text`, given the turn's committed steps. */
export function visibleText(state: TurnStream, committed: readonly CommittedStep[]): string {
  const stored = significant(
    committed
      .filter((step) => !state.seen.includes(step.id))
      .map((step) => step.markdown)
      .join(""),
  );
  const received = significant(state.received);
  const overlap = longestOverlap(received.chars, stored.chars);
  const cut = overlap === 0 ? 0 : (received.ends[overlap - 1] ?? 0);
  return state.received.slice(cut).trimStart();
}

/** A text's non-whitespace characters, with where each one ends in the text. */
function significant(text: string): { chars: string[]; ends: number[] } {
  const chars: string[] = [];
  const ends: number[] = [];
  let index = 0;
  for (const char of text) {
    index += char.length;
    if (/\s/.test(char)) continue;
    chars.push(char);
    ends.push(index);
  }
  return { chars, ends };
}

/** The length of the longest prefix of `head` that is also a suffix of `tail` (KMP). */
export function longestOverlap(head: readonly string[], tail: readonly string[]): number {
  // `null` separates the two, so a match never runs across from `head` into `tail`.
  const sequence: (string | null)[] = [...head, null, ...tail];
  const border: number[] = [0];
  for (let index = 1; index < sequence.length; index += 1) {
    let length = border[index - 1] ?? 0;
    while (length > 0 && sequence[index] !== sequence[length]) length = border[length - 1] ?? 0;
    if (sequence[index] === sequence[length]) length += 1;
    border.push(length);
  }
  return border.at(-1) ?? 0;
}
