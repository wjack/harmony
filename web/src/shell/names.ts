import type { MemberView } from "../api/types";

const HONORIFICS = new Set(["dr", "dr.", "prof", "prof.", "mr", "mr.", "ms", "ms.", "mrs", "mrs."]);

function nameParts(name: string): string[] {
  return name
    .trim()
    .split(/\s+/)
    .filter((part) => part !== "" && !HONORIFICS.has(part.toLowerCase()));
}

/** "Dr. Helen Ward" → "Helen": how the demo addresses people ("switch to Helen"). */
export function firstName(name: string): string {
  return nameParts(name)[0] ?? name;
}

/** "Dr. Helen Ward" → "HW"; `count` 1 gives a monogram ("Northlake Core" → "N"). */
export function initials(name: string, count = 2): string {
  const parts = nameParts(name);
  if (parts.length === 0) return "?";
  const picked = count === 1 || parts.length === 1 ? [parts[0]] : [parts[0], parts.at(-1)];
  return picked
    .map((part) => part?.[0] ?? "")
    .join("")
    .toUpperCase();
}

const ROLE_LABELS: Record<MemberView["role"], string> = {
  admin: "Admin",
  member: "Member",
};

export function roleLabel(role: MemberView["role"]): string {
  return ROLE_LABELS[role];
}

/** "Northlake Characterization Core" → "Northlake"; two-word names stay whole. */
export function shortOrgName(name: string): string {
  const words = name.trim().split(/\s+/);
  return words.length <= 2 ? name : (words[0] ?? name);
}

/** The account switcher's second line: "Core director · Northlake". */
export function memberSubtitle(member: MemberView): string {
  return `${member.title ?? roleLabel(member.role)} · ${shortOrgName(member.org.name)}`;
}
