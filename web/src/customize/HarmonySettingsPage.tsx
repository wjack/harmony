import { Lock } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";

import { useSession } from "../app/queries";
import { showRail } from "../shell/sidebarStore";
import { toast } from "../shell/toasts";
import { Button, Spark } from "../shell/ui";
import { useMediaQuery } from "../shell/useMediaQuery";
import {
  isForbidden,
  isStaleSave,
  SaveFailed,
  saveErrorMessage,
  useHarmonySettings,
  useSaveSettings,
  useSettingsLive,
} from "./api";
import { AutonomyPane } from "./AutonomyPane";
import { CUSTOMIZE_NAV, CustomizeNav } from "./CustomizeNav";
import {
  afterSave,
  applyEdits,
  changedFields,
  conflicts,
  draftProblem,
  dropConflicts,
  toUpdate,
  touchesPrompt,
  touchesSharing,
  trustedChanges,
  withPatch,
  type DraftEdits,
  type DraftPatch,
  type SettingsField,
} from "./draft";
import { Callout } from "./fields";
import { InstructionsPane } from "./InstructionsPane";
import { DIRECTORY, FIELD_NAMES, listOf } from "./labels";
import { LeaveGuard } from "./LeaveGuard";
import { MODE_INFO } from "./modes";
import type { PaneProps } from "./pane";
import { ProfilePane } from "./ProfilePane";
import { SaveBar } from "./SaveBar";
import { SectionList } from "./SectionList";
import { isSection, SECTIONS, sectionInfo } from "./sections";
import { SharingPane } from "./SharingPane";
import { TeamPane } from "./TeamPane";
import type { HarmonySection, HarmonySettingsView } from "./types";

/** Customize shows Claude's sidebar as its icon rail (UI_REFERENCE.md §6), and puts it back after. */
function useSidebarRail() {
  useEffect(showRail, []);
}

function adminNames(view: HarmonySettingsView): string {
  const names = view.members.filter((member) => member.role === "admin").map((member) => member.member.name);
  if (names.length === 0) return "an admin";
  if (names.length === 1) return names[0] ?? "an admin";
  return `${names.slice(0, -1).join(", ")} or ${names.at(-1) ?? ""}`;
}

function listMeta(view: HarmonySettingsView, edits: DraftEdits): Partial<Record<HarmonySection, string>> {
  const draft = applyEdits(view, edits);
  const trusted = Object.keys(draft.trusted).length;
  return {
    profile: DIRECTORY[draft.directoryVisibility].label,
    sharing: trusted > 0 ? `${String(trusted)} trusted` : undefined,
    autonomy: MODE_INFO[draft.autonomy].label,
    team: String(view.members.length),
  };
}

function Pane({ section, focusOrgId, ...props }: PaneProps & { section: HarmonySection; focusOrgId: string | null }) {
  switch (section) {
    case "profile":
      return <ProfilePane {...props} />;
    case "instructions":
      return <InstructionsPane {...props} />;
    case "sharing":
      return <SharingPane {...props} focusOrgId={focusOrgId} />;
    case "autonomy":
      return <AutonomyPane {...props} />;
    case "team":
      return <TeamPane {...props} />;
  }
}

/** The toast after a save: when what changed reaches the representative. */
function savedText(changed: readonly SettingsField[]): string {
  if (touchesPrompt(changed)) return "Settings saved. Live threads keep the profile they started with and get a notice.";
  if (touchesSharing(changed)) return "Settings saved. Live threads are told what they may now share.";
  if (changed.includes("sendWithoutConfirmation")) {
    return "Settings saved. Chats that follow your organization's default use it for their next request.";
  }
  return "Settings saved. They apply at the next decision on every thread.";
}

interface SettingsBodyProps {
  view: HarmonySettingsView;
  section: HarmonySection;
  focusOrgId: string | null;
  onSection: (section: HarmonySection) => void;
  /** Customize's own column is hidden (a narrower window). */
  compact: boolean;
}

/** What the save bar says: a failure, else what to know before saving, else nothing. */
function saveMessages(
  view: HarmonySettingsView,
  edits: DraftEdits,
  saveError: unknown,
  invalid: string | null,
): { problem: string | null; notice: string | null; clashes: boolean } {
  const clashing = conflicts(view, edits);
  const failed = saveError === null || isStaleSave(saveError) ? null : saveErrorMessage(saveError);
  let notice: string | null = null;
  if (clashing.length > 0) {
    notice = `Another admin also changed ${listOf(clashing.map((field) => FIELD_NAMES[field]))}. Saving replaces their version.`;
  } else if (isStaleSave(saveError)) {
    notice = "Another admin saved first. Your edits are on top of their version; save again to apply them.";
  }
  return { problem: invalid ?? failed, notice, clashes: clashing.length > 0 };
}

function SettingsBody({ view, section, focusOrgId, onSection, compact }: SettingsBodyProps) {
  const save = useSaveSettings(view);
  const [edits, setEdits] = useState<DraftEdits>({});
  // Kept here rather than read from the mutation: editing clears the message, and
  // nothing may reset the mutation while it's in flight (its result is still applied).
  const [saveError, setSaveError] = useState<unknown>(null);

  const editable = view.editable;
  const saving = save.isPending;
  const draft = applyEdits(view, edits);
  const changed = changedFields(view, edits);
  const invalid = changed.length > 0 ? draftProblem(draft) : null;
  const info = sectionInfo(section);
  const unsaved = new Set(SECTIONS.filter((pane) => pane.fields.some((field) => changed.includes(field))).map((pane) => pane.id));
  const messages = saveMessages(view, edits, saveError, invalid);

  const edit = (patch: DraftPatch) => {
    if (!editable) return;
    setSaveError(null);
    setEdits((current) => withPatch(view, current, patch));
  };

  const onSave = () => {
    const update = toUpdate(view, edits);
    const changes = trustedChanges(view, edits);
    if (saving || (update === null && changes.length === 0) || invalid !== null) return;
    const sent = edits;
    const text = savedText(changed);
    setSaveError(null);
    save.mutateAsync({ update, changes }).then(
      (saved) => {
        setEdits((current) => afterSave(current, sent, saved));
        toast({ key: "harmony-settings", text });
      },
      (error: unknown) => {
        setSaveError(error);
        // Someone else's role change: the edits can never be saved, so drop them.
        if (isForbidden(error)) setEdits({});
        // Whatever landed before the failure is saved; the rest stays unsaved.
        else if (error instanceof SaveFailed) setEdits((current) => afterSave(current, sent, error.saved));
      },
    );
  };

  const onCancel = () => {
    if (saving) return;
    setSaveError(null);
    setEdits({});
  };

  return (
    <>
      <LeaveGuard dirty={editable && changed.length > 0} />
      <SectionList
        org={view.org}
        section={section}
        onSelect={onSection}
        meta={listMeta(view, edits)}
        unsaved={unsaved}
        editable={editable}
        compact={compact}
      />
      {/* `w-0`: the pane takes what's left without its content (a long trusted
          organization's name) widening the app frame past the window. */}
      <div className="flex w-0 min-w-0 flex-1 flex-col">
        {/* Above the scroll area, so a viewer never scrolls the reason away. */}
        {!editable && (
          <div className="max-w-[808px] shrink-0 px-6 pt-3">
            <Callout icon={<Lock size={14} strokeWidth={1.75} />}>
              Only admins can change these settings. Ask {adminNames(view)} if something needs to change.
            </Callout>
          </div>
        )}
        <div className="scrollbar-quiet relative flex min-h-0 flex-1 flex-col overflow-y-auto" aria-label={info.label}>
          <div className="max-w-[808px] flex-1 px-6 pb-10">
            <header className="flex min-h-12 items-center gap-2.5 pt-2">
              <info.Icon size={18} strokeWidth={1.5} className="shrink-0 text-secondary" aria-hidden />
              <h2 className="min-w-0 flex-1 truncate text-[15px] leading-6 font-semibold text-ink">{info.label}</h2>
            </header>
            <p className="max-w-[680px] text-[13px] leading-5 text-secondary">{info.description}</p>
            <div className="mt-4">
              <Pane
                section={section}
                focusOrgId={focusOrgId}
                view={view}
                draft={draft}
                edit={edit}
                editable={editable}
                onSection={onSection}
              />
            </div>
          </div>
          {(changed.length > 0 || messages.problem !== null) && editable && (
            <SaveBar
              problem={messages.problem}
              notice={messages.notice}
              saving={saving}
              blocked={invalid !== null || changed.length === 0}
              notifiesLive={touchesPrompt(changed) || touchesSharing(changed)}
              onCancel={onCancel}
              onSave={onSave}
              onUseTheirs={
                messages.clashes
                  ? () => {
                      setEdits((current) => dropConflicts(view, current));
                    }
                  : undefined
              }
            />
          )}
        </div>
      </div>
    </>
  );
}

function Loading() {
  return (
    <div className="flex flex-1 items-center justify-center" aria-busy="true">
      <Spark size={28} animated label="Loading Harmony settings" />
    </div>
  );
}

function LoadError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
      <p className="max-w-[360px] text-sm text-secondary">Couldn&rsquo;t load your Harmony settings.</p>
      <Button onClick={onRetry}>Try again</Button>
    </div>
  );
}

/**
 * Customize → Harmony: the second-level nav, the list of panes and the open pane, for
 * the acting member's org. `?section=` picks the pane.
 */
export function HarmonySettingsScreen() {
  const session = useSession();
  const orgSlug = session.data?.member.org.slug ?? null;
  const settings = useHarmonySettings(orgSlug);
  useSettingsLive(orgSlug);
  const [params, setParams] = useSearchParams();
  const requested = params.get("section");
  const section: HarmonySection = isSection(requested) ? requested : "profile";
  // `?org=` names the organization a link came about, e.g. from the Requests inbox.
  const focusOrgId = section === "sharing" ? params.get("org") : null;
  const navColumn = useMediaQuery(CUSTOMIZE_NAV);

  const onSection = (next: HarmonySection) => {
    setParams(
      (current) => {
        const updated = new URLSearchParams(current);
        updated.set("section", next);
        updated.delete("org");
        return updated;
      },
      { replace: true },
    );
  };

  return (
    <div className="flex h-full min-h-0 bg-canvas">
      {navColumn && <CustomizeNav />}
      {settings.data !== undefined ? (
        // A different org (after an account switch) starts from a clean draft.
        <SettingsBody
          key={settings.data.org.id}
          view={settings.data}
          section={section}
          focusOrgId={focusOrgId}
          onSection={onSection}
          compact={!navColumn}
        />
      ) : settings.isError ? (
        <LoadError
          onRetry={() => {
            void settings.refetch();
          }}
        />
      ) : (
        <Loading />
      )}
    </div>
  );
}

/** The `/customize/harmony` route. */
export function HarmonySettingsPage() {
  useSidebarRail();
  return <HarmonySettingsScreen />;
}
