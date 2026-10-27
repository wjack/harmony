/** The panes of Customize → Harmony, grouped as the list column shows them. */
import { Gauge, IdCard, ScrollText, Share2, UsersRound, type LucideIcon } from "lucide-react";

import type { SettingsField } from "./draft";
import type { HarmonySection } from "./types";

export interface SectionInfo {
  id: HarmonySection;
  label: string;
  Icon: LucideIcon;
  description: string;
  /** The draft fields edited on this pane (for its unsaved dot). */
  fields: readonly SettingsField[];
}

export interface SectionGroup {
  label: string;
  sections: readonly SectionInfo[];
}

export const SECTION_GROUPS: readonly SectionGroup[] = [
  {
    label: "Representative",
    sections: [
      {
        id: "profile",
        label: "Public profile",
        Icon: IdCard,
        description:
          "What other organizations and the network's search read about you. A vague profile gets passed over.",
        fields: ["publicSummary", "directoryVisibility"],
      },
      {
        id: "instructions",
        label: "Instructions",
        Icon: ScrollText,
        description: "Your representative's private prompt: priorities, tone, and when to check with the team.",
        fields: ["instructions"],
      },
      {
        id: "sharing",
        label: "Sharing",
        Icon: Share2,
        description:
          "What your representative may pass on, in your own words. Internal Claude drafts every answer with it in mind. In Auto, Claude also checks each answer against it, and anything beyond it waits for a teammate. In Ask a teammate reviews every answer; in Skip nothing checks it.",
        fields: ["sharingDescription", "trusted"],
      },
      {
        id: "autonomy",
        label: "Autonomy",
        Icon: Gauge,
        description:
          "How much your representative does without a teammate, and whether requests Claude writes for your members wait for their review. A change applies at the next decision.",
        fields: ["autonomy", "sendWithoutConfirmation"],
      },
    ],
  },
  {
    label: "Team",
    sections: [
      {
        id: "team",
        label: "Team and routing",
        Icon: UsersRound,
        description: "Who answers when your representative checks with the team. Claude routes from your prompt.",
        fields: ["routingPrompt", "defaultAssigneeId", "profiles"],
      },
    ],
  },
];

export const SECTIONS: readonly SectionInfo[] = SECTION_GROUPS.flatMap((group) => group.sections);

export function isSection(value: string | null): value is HarmonySection {
  return SECTIONS.some((section) => section.id === value);
}

export function sectionInfo(id: HarmonySection): SectionInfo {
  for (const section of SECTIONS) if (section.id === id) return section;
  throw new Error(`unknown Harmony settings section: ${id}`);
}

/** Customize → Harmony's Sharing pane, opened at `orgId`'s trusted-organization row. */
export function sharingPath(orgId: string): string {
  return `/customize/harmony?section=sharing&org=${encodeURIComponent(orgId)}`;
}
