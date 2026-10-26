/** How Customize → Harmony words the settings' enum values and fields. */
import type { SettingsField } from "./draft";
import type { DirectoryVisibility } from "./types";

export const DIRECTORY: Readonly<Record<DirectoryVisibility, { label: string; description: string }>> = {
  public: { label: "Public", description: "Any organization on the network can find you." },
  communities: { label: "Communities only", description: "Only organizations in your communities can find you." },
  unlisted: { label: "Unlisted", description: "Only organizations you've already worked with." },
};

export const DIRECTORY_ORDER: readonly DirectoryVisibility[] = ["public", "communities", "unlisted"];

/** Each editable field, as the save bar names it ("Another admin also changed …"). */
export const FIELD_NAMES: Readonly<Record<SettingsField, string>> = {
  publicSummary: "the summary",
  instructions: "the instructions",
  sharingDescription: "what you share",
  autonomy: "the default mode",
  directoryVisibility: "the directory listing",
  routingPrompt: "the routing prompt",
  defaultAssigneeId: "the default assignee",
  trusted: "a trusted organization",
  profiles: "a member's profile",
};

/** "a", "a and b", "a, b and c". */
export function listOf(items: readonly string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1) ?? ""}`;
}
