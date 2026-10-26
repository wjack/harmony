/** Display formatting for the chat: dates, counts, greetings. */
import type { Calendar } from "../demo/worldClock";

/** "2m", "3h", "Nov 6": how long ago, compactly, as Linear and Claude show it. A date is
 * read off `calendar` (the session's world calendar), so it matches the World date. */
export function formatSince(iso: string, calendar: Calendar, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return calendar(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function greeting(hour: number): string {
  if (hour < 5) return "Good evening";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}
