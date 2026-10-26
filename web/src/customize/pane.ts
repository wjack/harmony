import type { DraftPatch, SettingsDraft } from "./draft";
import type { HarmonySection, HarmonySettingsView } from "./types";

/** What every Customize → Harmony pane gets: the saved view and the working draft. */
export interface PaneProps {
  view: HarmonySettingsView;
  /** The view with unsaved edits on top: what the inputs show. */
  draft: SettingsDraft;
  /** Whole new values for the fields a control changed; the page records them leaf by leaf. */
  edit: (patch: DraftPatch) => void;
  /** Admins edit; everyone else sees the same pane read-only (§9). */
  editable: boolean;
  /** Opens another pane: a cross-reference such as "set it per organization under Sharing". */
  onSection: (section: HarmonySection) => void;
}
