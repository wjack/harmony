/** Display formatting for the network view: waiting times, org kinds, counts. */

/** "just now", "4m", "2h", "3d": how long something has waited, compactly. */
export function formatWait(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

/** "4m ago", or "just now". */
export function formatAgo(iso: string, now: number = Date.now()): string {
  const wait = formatWait(iso, now);
  return wait === "just now" ? wait : `${wait} ago`;
}

// The seeded `orgs.kind` values read as the categories the canvas groups by (§11.6).
const KIND_LABELS: Readonly<Record<string, string>> = {
  research: "Research lab",
  characterization: "Characterization",
  fabrication: "Fabrication",
  synthesis: "Synthesis",
  computation: "Computation",
  data: "Data holder",
  funder: "Funder",
  testing: "Testing",
  logistics: "Logistics",
  procurement: "Procurement",
};

/** "characterization" → "Characterization"; unknown kinds are capitalized as-is. */
export function kindLabel(kind: string): string {
  const known = KIND_LABELS[kind];
  if (known !== undefined) return known;
  const words = kind.replace(/[-_]+/g, " ").trim();
  return words === "" ? "Organization" : words.charAt(0).toUpperCase() + words.slice(1);
}

/** "Northlake Characterization Core" → "Northlake"; two-word names stay whole. */
export function shortOrgName(name: string): string {
  const words = name.trim().split(/\s+/);
  return words.length <= 2 ? name : (words[0] ?? name);
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}
