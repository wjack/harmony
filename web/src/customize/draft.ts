/**
 * Unsaved edits to the Harmony settings, kept as field-level changes over the server's
 * view and turned into the minimal writes on save: one `PUT` for the org's own fields
 * and its members' profiles, and one call per trusted organization that changed.
 *
 * An edit records one leaf, never a whole compound value: a scalar field, whether an
 * organization is trusted, one trusted organization's mode or description, or one
 * member's profile. Replayed
 * onto a newer view (another admin's save arriving live, or the reload after a 409),
 * the edits keep what that admin changed elsewhere, including other trusted rows.
 *
 * Each leaf also keeps the server's value from when the admin first touched it (its
 * base). If the server's value has since moved to something else, both admins changed
 * the same thing: that's a conflict the save bar names, since saving replaces theirs.
 */
import type {
  Autonomy,
  DirectoryVisibility,
  HarmonySettingsUpdate,
  HarmonySettingsView,
  TrustedOrganizationCreate,
  TrustedOrganizationUpdate,
} from "./types";

export const SUMMARY_MAX = 2000;
export const PROSE_MAX = 20_000;

/** One trusted organization as the form edits it. */
export interface TrustedDraft {
  /** null: the same mode as the org's default. */
  autonomy: Autonomy | null;
  /** "Also share with them": added to the default description for this organization. */
  sharingDescription: string;
}

/** Every field Customize → Harmony can change, in the shape the form edits. */
export interface SettingsDraft {
  publicSummary: string;
  instructions: string;
  sharingDescription: string;
  autonomy: Autonomy;
  directoryVisibility: DirectoryVisibility;
  routingPrompt: string;
  defaultAssigneeId: string | null;
  /** Trusted organizations by counterparty org id. */
  trusted: Readonly<Record<string, TrustedDraft>>;
  /** Each member's profile (what they handle, in prose) by member id. */
  profiles: Readonly<Record<string, string>>;
}

export type SettingsField = keyof SettingsDraft;

/** What a pane hands `edit`: whole new values for the fields it touched. */
export type DraftPatch = Partial<SettingsDraft>;

type Value = string | boolean | null;

interface Leaf {
  value: Value;
  /** The server's value when this leaf was first edited. */
  base: Value;
}

/**
 * The unsaved edits, by path: a scalar field's name ("instructions"), whether an org
 * is trusted ("trusted/<org id>": true or false), one of its fields
 * ("trusted/<org id>/autonomy", "trusted/<org id>/sharing"), or a member's profile
 * ("profile/<member id>").
 */
export type DraftEdits = Readonly<Record<string, Leaf>>;

const SCALARS = [
  "publicSummary",
  "instructions",
  "sharingDescription",
  "autonomy",
  "directoryVisibility",
  "routingPrompt",
  "defaultAssigneeId",
] as const satisfies readonly SettingsField[];
type ScalarField = (typeof SCALARS)[number];

const TRUSTED_FIELDS = { autonomy: "autonomy", sharing: "sharingDescription" } as const;
type TrustedPathField = keyof typeof TRUSTED_FIELDS;

const NEW_TRUSTED: TrustedDraft = { autonomy: null, sharingDescription: "" };

export function draftFrom(view: HarmonySettingsView): SettingsDraft {
  return {
    publicSummary: view.public_summary,
    instructions: view.instructions,
    sharingDescription: view.sharing_description,
    autonomy: view.autonomy,
    directoryVisibility: view.directory_visibility,
    routingPrompt: view.routing_prompt,
    defaultAssigneeId: view.default_assignee?.id ?? null,
    trusted: Object.fromEntries(
      view.trusted_organizations.map((row) => [
        row.counterparty.id,
        { autonomy: row.autonomy, sharingDescription: row.sharing_description },
      ]),
    ),
    profiles: Object.fromEntries(view.members.map((member) => [member.member.id, member.profile])),
  };
}

function isScalar(path: string): path is ScalarField {
  return (SCALARS as readonly string[]).includes(path);
}

/** "trusted/<id>" → [id, null]; "trusted/<id>/sharing" → [id, "sharing"]; else null. */
function trustedPath(path: string): [orgId: string, field: TrustedPathField | null] | null {
  const [head, orgId, field] = path.split("/");
  if (head !== "trusted" || orgId === undefined) return null;
  if (field === undefined) return [orgId, null];
  return field === "autonomy" || field === "sharing" ? [orgId, field] : null;
}

/** "profile/<id>" → id; else null. */
function profilePath(path: string): string | null {
  const [head, memberId, rest] = path.split("/");
  return head === "profile" && memberId !== undefined && rest === undefined ? memberId : null;
}

/** A leaf's current value on the server (in `draft`, the server's draft). */
function serverValue(draft: SettingsDraft, path: string): Value {
  const memberId = profilePath(path);
  // A member who left the org has no profile to save.
  if (memberId !== null) return draft.profiles[memberId] ?? null;
  const trusted = trustedPath(path);
  if (trusted !== null) {
    const [orgId, field] = trusted;
    const row = draft.trusted[orgId];
    if (field === null) return row !== undefined;
    return (row ?? NEW_TRUSTED)[TRUSTED_FIELDS[field]];
  }
  return isScalar(path) ? draft[path] : null;
}

function setLeaf(edits: Map<string, Leaf>, server: SettingsDraft, path: string, value: Value): void {
  const now = serverValue(server, path);
  if (value === now) {
    // Nothing left to send for this leaf.
    edits.delete(path);
    return;
  }
  edits.set(path, { value, base: edits.get(path)?.base ?? now });
}

function patchTrusted(
  next: Map<string, Leaf>,
  server: SettingsDraft,
  current: SettingsDraft,
  trusted: Readonly<Record<string, TrustedDraft>>,
): void {
  for (const orgId of new Set([...Object.keys(current.trusted), ...Object.keys(trusted)])) {
    const before = current.trusted[orgId];
    const after = trusted[orgId];
    if ((before === undefined) !== (after === undefined)) setLeaf(next, server, `trusted/${orgId}`, after !== undefined);
    if (after === undefined) {
      // A removed row has nothing else left to save.
      next.delete(`trusted/${orgId}/autonomy`);
      next.delete(`trusted/${orgId}/sharing`);
      continue;
    }
    const was = before ?? NEW_TRUSTED;
    // A row added back starts fresh, so its fields are recorded even when unchanged.
    const fresh = before === undefined && server.trusted[orgId] !== undefined;
    if (fresh || after.autonomy !== was.autonomy) setLeaf(next, server, `trusted/${orgId}/autonomy`, after.autonomy);
    if (fresh || after.sharingDescription !== was.sharingDescription) {
      setLeaf(next, server, `trusted/${orgId}/sharing`, after.sharingDescription);
    }
  }
}

/** The edits after a pane's `patch`, recorded leaf by leaf against the current draft. */
export function withPatch(view: HarmonySettingsView, edits: DraftEdits, patch: DraftPatch): DraftEdits {
  const server = draftFrom(view);
  const next = new Map(Object.entries(edits));
  for (const field of SCALARS) {
    if (field in patch) setLeaf(next, server, field, patch[field] ?? null);
  }
  if (patch.trusted !== undefined) patchTrusted(next, server, applyEdits(view, edits), patch.trusted);
  for (const [memberId, profile] of Object.entries(patch.profiles ?? {})) {
    if (memberId in server.profiles) setLeaf(next, server, `profile/${memberId}`, profile);
  }
  return Object.fromEntries(next);
}

/** The form's current values: the server's, with the edits replayed on top. */
export function applyEdits(view: HarmonySettingsView, edits: DraftEdits): SettingsDraft {
  const draft: SettingsDraft = { ...draftFrom(view) };
  const trusted: Record<string, TrustedDraft> = { ...draft.trusted };
  const profiles: Record<string, string> = { ...draft.profiles };
  const removed = new Set<string>();
  for (const [path, { value }] of Object.entries(edits)) {
    const memberId = profilePath(path);
    if (memberId !== null) {
      if (memberId in profiles && typeof value === "string") profiles[memberId] = value;
      continue;
    }
    const target = trustedPath(path);
    if (target === null) {
      if (isScalar(path)) Object.assign(draft, { [path]: value });
      continue;
    }
    const [orgId, field] = target;
    if (field === null) {
      if (value === false) removed.add(orgId);
      else trusted[orgId] ??= NEW_TRUSTED;
      continue;
    }
    // A field edit on a row another admin removed puts it back, as saving would.
    const row = trusted[orgId] ?? NEW_TRUSTED;
    trusted[orgId] = { ...row, [TRUSTED_FIELDS[field]]: value };
  }
  draft.trusted = Object.fromEntries(Object.entries(trusted).filter(([orgId]) => !removed.has(orgId)));
  draft.profiles = profiles;
  return draft;
}

function sameTrusted(a: TrustedDraft | undefined, b: TrustedDraft | undefined): boolean {
  if (a === undefined || b === undefined) return a === b;
  return a.autonomy === b.autonomy && a.sharingDescription === b.sharingDescription;
}

/** Trusted organizations whose row was added, removed or changed. */
function changedTrusted(base: SettingsDraft, next: SettingsDraft): string[] {
  const ids = new Set([...Object.keys(base.trusted), ...Object.keys(next.trusted)]);
  return [...ids].filter((orgId) => !sameTrusted(base.trusted[orgId], next.trusted[orgId]));
}

/** Members whose profile changed. */
function changedProfiles(base: SettingsDraft, next: SettingsDraft): string[] {
  return Object.keys(base.profiles).filter((memberId) => base.profiles[memberId] !== next.profiles[memberId]);
}

/** Which fields differ from the server's view (empty when there is nothing to save). */
export function changedFields(view: HarmonySettingsView, edits: DraftEdits): SettingsField[] {
  const base = draftFrom(view);
  const next = applyEdits(view, edits);
  const changed: SettingsField[] = SCALARS.filter((field) => base[field] !== next[field]);
  if (changedTrusted(base, next).length > 0) changed.push("trusted");
  if (changedProfiles(base, next).length > 0) changed.push("profiles");
  return changed;
}

function fieldOf(path: string): SettingsField {
  if (profilePath(path) !== null) return "profiles";
  return trustedPath(path) === null ? (path as SettingsField) : "trusted";
}

/**
 * Fields another admin changed to something else after this admin started editing
 * them: saving replaces their value. The same change on both sides isn't a conflict.
 */
export function conflicts(view: HarmonySettingsView, edits: DraftEdits): SettingsField[] {
  const server = draftFrom(view);
  const fields = new Set<SettingsField>();
  for (const [path, { value, base }] of Object.entries(edits)) {
    const now = serverValue(server, path);
    if (now !== base && now !== value) fields.add(fieldOf(path));
  }
  return [...fields];
}

/** The edits without those that conflict ("use theirs"). */
export function dropConflicts(view: HarmonySettingsView, edits: DraftEdits): DraftEdits {
  const server = draftFrom(view);
  return Object.fromEntries(
    Object.entries(edits).filter(([path, { value, base }]) => {
      const now = serverValue(server, path);
      return now === base || now === value;
    }),
  );
}

/**
 * The edits left after a save of `sent` came back as `saved`: what it sent is the
 * server's now, and whatever changed while it was in flight stays unsaved, based on the
 * saved value.
 */
export function afterSave(current: DraftEdits, sent: DraftEdits, saved: HarmonySettingsView): DraftEdits {
  const server = draftFrom(saved);
  const left: Record<string, Leaf> = {};
  for (const [path, leaf] of Object.entries(current)) {
    const now = serverValue(server, path);
    if (leaf.value === now) continue;
    left[path] = path in sent ? { value: leaf.value, base: now } : leaf;
  }
  return left;
}

/** The public summary and instructions write a new settings version; the rest is live policy. */
const PROMPT_FIELDS: ReadonlySet<SettingsField> = new Set(["publicSummary", "instructions"]);

export function touchesPrompt(fields: readonly SettingsField[]): boolean {
  return fields.some((field) => PROMPT_FIELDS.has(field));
}

/** What every live thread with the changed counterparties is told on its next turn. */
export function touchesSharing(fields: readonly SettingsField[]): boolean {
  return fields.includes("sharingDescription") || fields.includes("trusted");
}

/**
 * The `PUT /harmony` body for the org's own fields and its members' profiles: only what
 * changed, plus its version.
 */
export function toUpdate(view: HarmonySettingsView, edits: DraftEdits): HarmonySettingsUpdate | null {
  const changed = changedFields(view, edits).filter((field) => field !== "trusted");
  if (changed.length === 0) return null;
  const base = draftFrom(view);
  const next = applyEdits(view, edits);
  const update: HarmonySettingsUpdate = { policy_version: view.policy_version };
  for (const field of changed) {
    switch (field) {
      case "publicSummary":
        update.public_summary = next.publicSummary.trim();
        break;
      case "instructions":
        update.instructions = next.instructions;
        break;
      case "sharingDescription":
        update.sharing_description = next.sharingDescription;
        break;
      case "autonomy":
        update.autonomy = next.autonomy;
        break;
      case "directoryVisibility":
        update.directory_visibility = next.directoryVisibility;
        break;
      case "routingPrompt":
        update.routing_prompt = next.routingPrompt;
        break;
      case "defaultAssigneeId":
        update.default_assignee_id = next.defaultAssigneeId;
        break;
      case "profiles":
        update.member_profiles = Object.fromEntries(
          changedProfiles(base, next).map((memberId) => [memberId, next.profiles[memberId] ?? ""]),
        );
        break;
    }
  }
  return update;
}

/** One write to the trusted organizations table. */
export type TrustedChange =
  | { kind: "add"; orgId: string; body: TrustedOrganizationCreate }
  | { kind: "update"; orgId: string; body: TrustedOrganizationUpdate }
  | { kind: "remove"; orgId: string; version: number };

/** The trusted-organization writes for the edits, each with the row version it was based on. */
export function trustedChanges(view: HarmonySettingsView, edits: DraftEdits): TrustedChange[] {
  const base = draftFrom(view);
  const next = applyEdits(view, edits);
  const versions = new Map(view.trusted_organizations.map((row) => [row.counterparty.id, row.version]));
  return changedTrusted(base, next).map((orgId): TrustedChange => {
    const was = base.trusted[orgId];
    const row = next.trusted[orgId];
    const version = versions.get(orgId) ?? 0;
    if (row === undefined) return { kind: "remove", orgId, version };
    if (was === undefined) {
      return {
        kind: "add",
        orgId,
        body: { counterparty_org_id: orgId, autonomy: row.autonomy, sharing_description: row.sharingDescription },
      };
    }
    const body: TrustedOrganizationUpdate = { version };
    if (row.autonomy !== was.autonomy) body.autonomy = row.autonomy;
    if (row.sharingDescription !== was.sharingDescription) body.sharing_description = row.sharingDescription;
    return { kind: "update", orgId, body };
  });
}

/** What can't be saved as typed; null when the draft is valid. */
export function draftProblem(draft: SettingsDraft): string | null {
  if (draft.publicSummary.trim() === "") return "The public profile needs a summary.";
  if (draft.publicSummary.length > SUMMARY_MAX) return "Keep the profile summary under 2,000 characters.";
  if (draft.instructions.length > PROSE_MAX) return "Keep the instructions under 20,000 characters.";
  if (draft.sharingDescription.length > PROSE_MAX) return "Keep what you share under 20,000 characters.";
  if (Object.values(draft.trusted).some((row) => row.sharingDescription.length > PROSE_MAX)) {
    return "Keep what you share with each organization under 20,000 characters.";
  }
  if (draft.routingPrompt.length > PROSE_MAX) return "Keep the routing prompt under 20,000 characters.";
  if (Object.values(draft.profiles).some((profile) => profile.length > PROSE_MAX)) {
    return "Keep each member's profile under 20,000 characters.";
  }
  return null;
}
