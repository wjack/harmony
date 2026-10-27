/**
 * What an `@mention` in a comment is, by the server's own rule (`parse_mentions`
 * in `domain/notes.py`), so the composer, the transcript and the fixtures agree with
 * what actually shares a thread: a lowercase slug after an `@` that doesn't follow a
 * word character or another `@` (so an email address isn't one). Only a slug that names
 * a member of the note's org shares anything; the server drops the rest.
 */

/** `(?<![\w@])@([a-z0-9]+(?:-[a-z0-9]+)*)`, with Python's Unicode `\w`. */
const MENTION = /(?<![\p{L}\p{N}_@])@([a-z0-9]+(?:-[a-z0-9]+)*)/gu;

/** The `@partial` being typed just before the caret, by the same rule, for suggestions. */
const PARTIAL = /(?<![\p{L}\p{N}_@])@([a-z0-9-]*)$/iu;

/** The distinct slugs mentioned in `body`, in order. */
export function mentionedSlugs(body: string): string[] {
  return [...new Set(Array.from(body.matchAll(MENTION), (match) => match[1] ?? ""))];
}

/** Whether `body` mentions one of `members`, which makes it only a note (3.9). */
export function mentionsMember(body: string, members: readonly { slug: string }[]): boolean {
  const slugs = new Set(members.map((member) => member.slug));
  return mentionedSlugs(body).some((slug) => slugs.has(slug));
}

/** The `@partial` typed at `caret`, if any: where its `@` is, and the partial lowercased. */
export function mentionAt(text: string, caret: number): { start: number; query: string } | null {
  const match = PARTIAL.exec(text.slice(0, caret));
  if (match === null) return null;
  const query = match[1] ?? "";
  return { start: caret - query.length - 1, query: query.toLowerCase() };
}

export interface BodyPart {
  text: string;
  /** The part is a mention of one of the slugs given. */
  mention: boolean;
}

/** `body` cut around its mentions of `slugs` (a note's stored `mentions`); other `@` text stays plain. */
export function mentionParts(body: string, slugs: ReadonlySet<string>): BodyPart[] {
  const parts: BodyPart[] = [];
  let from = 0;
  for (const match of body.matchAll(MENTION)) {
    if (!slugs.has(match[1] ?? "")) continue;
    if (match.index > from) parts.push({ text: body.slice(from, match.index), mention: false });
    parts.push({ text: match[0], mention: true });
    from = match.index + match[0].length;
  }
  if (from < body.length) parts.push({ text: body.slice(from), mention: false });
  return parts;
}
