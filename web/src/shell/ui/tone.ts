// Muted, earthy fills that sit inside Claude's palette. The same org always gets the
// same one, so a member's avatar and their org's monogram match everywhere.
const TONES = ["#5f7a63", "#8a6a55", "#5b6b85", "#7c5f7a", "#9a7b3f", "#4f7b7b", "#8b5e5e", "#6b6b8f"] as const;

function hash(text: string): number {
  let value = 0;
  for (let index = 0; index < text.length; index += 1) {
    value = (value * 31 + text.charCodeAt(index)) >>> 0;
  }
  return value;
}

/** The fill for anything keyed on `seed` (an org slug, usually). */
export function toneFor(seed: string): string {
  return TONES[hash(seed) % TONES.length] ?? TONES[0];
}
