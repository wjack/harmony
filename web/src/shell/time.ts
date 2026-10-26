const UNITS: readonly [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 86_400],
  ["month", 30 * 86_400],
  ["week", 7 * 86_400],
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
  ["second", 1],
];

const FORMAT = new Intl.RelativeTimeFormat("en", { numeric: "always" });

/** "23 seconds ago", "1 day ago": the Chats list's "Last message …" phrasing. */
export function timeAgo(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1_000));
  if (seconds < 5) return "just now";
  for (const [unit, size] of UNITS) {
    if (seconds >= size) return FORMAT.format(-Math.floor(seconds / size), unit);
  }
  return "just now";
}
