import { describe, expect, it } from "vitest";

// The server's entity types (events/schemas.py `EntityType`), read as text.
import serverSchemas from "../../../src/harmony/events/schemas.py?raw";
import { createFixtureApi } from "../fixtures";
import { customizeRoutes, northlakeSettings } from "../fixtures/customize";
import { members, nonAdmin, orgs } from "../fixtures/shell";
import { SETTINGS_ENTITIES } from "./api";
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
  type DraftPatch,
} from "./draft";
import type { HarmonySettingsView } from "./types";

const view = northlakeSettings;
const pacific = orgs.pacific.id;
const vantage = orgs.vantage.id;
const henderson = orgs.henderson.id;

const patched = (patch: DraftPatch, base: HarmonySettingsView = view) => withPatch(base, {}, patch);
const trustedOf = (base: HarmonySettingsView = view) => applyEdits(base, {}).trusted;

describe("draft", () => {
  it("has nothing to save until something changes", () => {
    expect(changedFields(view, {})).toEqual([]);
    expect(toUpdate(view, {})).toBeNull();
    expect(trustedChanges(view, {})).toEqual([]);
    // An edit back to the saved value isn't a change either.
    expect(toUpdate(view, patched({ autonomy: view.autonomy, trusted: { ...trustedOf() } }))).toBeNull();
    expect(withPatch(view, patched({ instructions: "x" }), { instructions: view.instructions })).toEqual({});
  });

  it("sends only what changed, with the version it was based on", () => {
    const update = toUpdate(view, patched({ autonomy: "skip", sharingDescription: "Prices only." }));
    expect(update).toEqual({ policy_version: 7, autonomy: "skip", sharing_description: "Prices only." });
    expect(toUpdate(view, patched({ publicSummary: "  Northlake Core.  ", defaultAssigneeId: members.raj.id }))).toEqual({
      policy_version: 7,
      public_summary: "Northlake Core.",
      default_assignee_id: members.raj.id,
    });
    const sends = patched({ sendWithoutConfirmation: true });
    expect(toUpdate(view, sends)).toEqual({ policy_version: 7, send_without_confirmation: true });
    const sendFields = changedFields(view, sends);
    expect([touchesPrompt(sendFields), touchesSharing(sendFields)]).toEqual([false, false]);
  });

  it("turns trusted-organization edits into one write per row", () => {
    const edits = patched({
      trusted: {
        [vantage]: { autonomy: null, sharingDescription: "" },
        [henderson]: { autonomy: "skip", sharingDescription: "Reserve slots." },
      },
    });
    expect(toUpdate(view, edits)).toBeNull();
    expect(changedFields(view, edits)).toEqual(["trusted"]);
    expect(trustedChanges(view, edits)).toEqual([
      { kind: "remove", orgId: pacific, version: 1 },
      { kind: "update", orgId: vantage, body: { version: 1, autonomy: null } },
      {
        kind: "add",
        orgId: henderson,
        body: { counterparty_org_id: henderson, autonomy: "skip", sharing_description: "Reserve slots." },
      },
    ]);
  });

  it("forgets a row's edits when it's removed, and starts it fresh when it's added back", () => {
    const rows = trustedOf();
    const edited = patched({ trusted: { ...rows, [vantage]: { autonomy: "auto", sharingDescription: "More." } } });
    const rest = Object.fromEntries(Object.entries(rows).filter(([orgId]) => orgId !== vantage));
    const removed = withPatch(view, edited, { trusted: rest });
    expect(Object.keys(removed)).toEqual([`trusted/${vantage}`]);
    const back = withPatch(view, removed, { trusted: { ...rest, [vantage]: { autonomy: null, sharingDescription: "" } } });
    expect(trustedChanges(view, back)).toEqual([{ kind: "update", orgId: vantage, body: { version: 1, autonomy: null } }]);
  });

  it("sends only the members whose profile changed, in the same PUT", () => {
    const raj = members.raj.id;
    const edits = patched({ profiles: { [raj]: "XRD and SEM queues." } });
    expect(changedFields(view, edits)).toEqual(["profiles"]);
    expect(toUpdate(view, edits)).toEqual({ policy_version: 7, member_profiles: { [raj]: "XRD and SEM queues." } });
    expect(toUpdate(view, withPatch(view, edits, { profiles: { [raj]: applyEdits(view, {}).profiles[raj] ?? "" } }))).toBeNull();
    // Someone outside the org has no profile here to edit.
    expect(patched({ profiles: { [members.alex.id]: "Not ours." } })).toEqual({});
    expect(draftProblem(applyEdits(view, patched({ profiles: { [raj]: "x".repeat(20_001) } })))).toMatch(/profile/);
  });

  it("knows which edits reach the prompt, which reach live threads as a notice, and which are live policy", () => {
    expect(touchesPrompt(changedFields(view, patched({ publicSummary: "Briefly." })))).toBe(true);
    expect(touchesPrompt(changedFields(view, patched({ routingPrompt: "Everything to Helen." })))).toBe(false);
    expect(touchesSharing(changedFields(view, patched({ sharingDescription: "Prices." })))).toBe(true);
    expect(touchesSharing(changedFields(view, patched({ autonomy: "ask" })))).toBe(false);
    const profile = changedFields(view, patched({ profiles: { [members.raj.id]: "Queues." } }));
    expect([touchesPrompt(profile), touchesSharing(profile)]).toEqual([false, false]);
  });

  it("refuses a draft the API would refuse", () => {
    expect(draftProblem(applyEdits(view, {}))).toBeNull();
    expect(draftProblem(applyEdits(view, patched({ publicSummary: " " })))).toMatch(/summary/);
    expect(draftProblem(applyEdits(view, patched({ publicSummary: "x".repeat(2001) })))).toMatch(/2,000/);
    const long = "x".repeat(20_001);
    expect(draftProblem(applyEdits(view, patched({ trusted: { [vantage]: { autonomy: null, sharingDescription: long } } })))).toMatch(
      /each organization/,
    );
  });
});

describe("another admin's save underneath", () => {
  // v8: another admin changed the routing prompt and put Vantage on Auto.
  const theirs: HarmonySettingsView = {
    ...view,
    policy_version: view.policy_version + 1,
    routing_prompt: "Everything to Helen.",
    trusted_organizations: view.trusted_organizations.map((row) =>
      row.counterparty.id === vantage ? { ...row, autonomy: "auto", version: 2 } : row,
    ),
  };

  it("keeps what they changed elsewhere, including other trusted rows", () => {
    const ours = patched({
      instructions: "Ours.",
      trusted: { ...trustedOf(), [pacific]: { autonomy: "skip", sharingDescription: "Everything." } },
    });
    expect(toUpdate(theirs, ours)).toEqual({ policy_version: 8, instructions: "Ours." });
    expect(applyEdits(theirs, ours).trusted[vantage]?.autonomy).toBe("auto");
    expect(trustedChanges(theirs, ours)).toEqual([
      { kind: "update", orgId: pacific, body: { version: 1, autonomy: "skip", sharing_description: "Everything." } },
    ]);
    expect(conflicts(theirs, ours)).toEqual([]);
  });

  it("names a field both changed, and lets ours go", () => {
    const ours = patched({
      routingPrompt: "Ours.",
      autonomy: "ask",
      trusted: { ...trustedOf(), [vantage]: { autonomy: "skip", sharingDescription: "" } },
    });
    expect(conflicts(theirs, ours)).toEqual(["routingPrompt", "trusted"]);
    // Saving replaces theirs, with the row version they wrote.
    expect(trustedChanges(theirs, ours)).toEqual([{ kind: "update", orgId: vantage, body: { version: 2, autonomy: "skip" } }]);
    const kept = dropConflicts(theirs, ours);
    expect(applyEdits(theirs, kept)).toMatchObject({ routingPrompt: "Everything to Helen.", autonomy: "ask" });
    expect(applyEdits(theirs, kept).trusted[vantage]?.autonomy).toBe("auto");
    // The same change on both sides isn't a clash.
    expect(conflicts({ ...theirs, routing_prompt: "Ours." }, patched({ routingPrompt: "Ours." }))).toEqual([]);
  });

  it("names a profile both changed", () => {
    const raj = members.raj.id;
    const ours = patched({ profiles: { [raj]: "Ours." } });
    const edited = (profile: string): HarmonySettingsView => ({
      ...view,
      members: view.members.map((member) => (member.member.id === raj ? { ...member, profile } : member)),
    });
    expect(conflicts(edited("Theirs."), ours)).toEqual(["profiles"]);
    expect(conflicts(edited("Ours."), ours)).toEqual([]);
    expect(applyEdits(edited("Theirs."), dropConflicts(edited("Theirs."), ours)).profiles[raj]).toBe("Theirs.");
  });

  it("puts back a row they removed if ours edits it, since saving would", () => {
    const gone: HarmonySettingsView = {
      ...view,
      trusted_organizations: view.trusted_organizations.filter((row) => row.counterparty.id !== pacific),
    };
    const ours = patched({ trusted: { ...trustedOf(), [pacific]: { autonomy: null, sharingDescription: "More." } } });
    expect(conflicts(gone, ours)).toEqual(["trusted"]);
    expect(trustedChanges(gone, ours)).toEqual([
      { kind: "add", orgId: pacific, body: { counterparty_org_id: pacific, autonomy: null, sharing_description: "More." } },
    ]);
  });
});

describe("afterSave", () => {
  it("drops what was saved and keeps, rebased, what changed while the save was in flight", () => {
    const sent = patched({ autonomy: "ask", instructions: "A" });
    const current = withPatch(view, sent, { instructions: "AB" });
    const saved: HarmonySettingsView = { ...view, autonomy: "ask", instructions: "A", policy_version: 8 };
    const left = afterSave(current, sent, saved);
    expect(changedFields(saved, left)).toEqual(["instructions"]);
    expect(applyEdits(saved, left).instructions).toBe("AB");
    // Its base is now the saved text: the admin's own save isn't someone else's change.
    expect(conflicts(saved, left)).toEqual([]);
    expect(conflicts({ ...saved, instructions: "C" }, left)).toEqual(["instructions"]);
  });
});

describe("live refresh", () => {
  it("listens only for entity types the server emits", () => {
    const literal = /EntityType = Literal\[([^\]]*)\]/.exec(serverSchemas)?.[1] ?? "";
    const known = new Set([...literal.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]));
    expect(known.size).toBeGreaterThan(10);
    for (const type of SETTINGS_ENTITIES) expect(known).toContain(type);
    expect(SETTINGS_ENTITIES).toContain("trusted_organization");
  });
});

describe("settings fixtures", () => {
  const path = { params: { path: { slug: "northlake" } } };

  it("lets an admin save, then refuses a stale version", async () => {
    const api = createFixtureApi(customizeRoutes(members.helen));
    const saved = await api.PUT("/api/orgs/{slug}/harmony", { ...path, body: { policy_version: 7, autonomy: "ask" } });
    expect(saved.data?.autonomy).toBe("ask");
    expect(saved.data?.policy_version).toBe(8);
    const stale = await api.PUT("/api/orgs/{slug}/harmony", { ...path, body: { policy_version: 7, autonomy: "auto" } });
    expect(stale.response.status).toBe(409);
  });

  it("versions prompt saves apart from policy, as the API does", async () => {
    const api = createFixtureApi(customizeRoutes(members.helen));
    const saved = await api.PUT("/api/orgs/{slug}/harmony", { ...path, body: { policy_version: 7, instructions: "Hi." } });
    expect(saved.data).toMatchObject({ policy_version: 7, settings_version: 4, instructions: "Hi." });
    const same = await api.PUT("/api/orgs/{slug}/harmony", { ...path, body: { policy_version: 7, instructions: "Hi." } });
    expect(same.data?.settings_version).toBe(4);
    const sharing = await api.PUT("/api/orgs/{slug}/harmony", { ...path, body: { policy_version: 7, sharing_description: "Prices." } });
    expect(sharing.data).toMatchObject({ policy_version: 8, settings_version: 4 });
  });

  it("saves members' profiles without moving the policy version", async () => {
    const api = createFixtureApi(customizeRoutes(members.helen));
    const body = { policy_version: 7, member_profiles: { [members.raj.id]: "XRD and SEM queues." } };
    const saved = await api.PUT("/api/orgs/{slug}/harmony", { ...path, body });
    expect(saved.data?.policy_version).toBe(7);
    expect(saved.data?.members.find((member) => member.member.id === members.raj.id)?.profile).toBe("XRD and SEM queues.");
  });

  it("adds, updates and removes trusted organizations, each at its own version", async () => {
    const api = createFixtureApi(customizeRoutes(members.helen));
    const rowPath = (orgId: string) => ({ params: { path: { slug: "northlake", counterparty_org_id: orgId } } });
    const added = await api.POST("/api/orgs/{slug}/trusted-organizations", {
      ...path,
      body: { counterparty_org_id: henderson, sharing_description: "Reserve slots." },
    });
    expect(added.data).toMatchObject({ counterparty: orgs.henderson, autonomy: null, version: 1 });
    const again = await api.POST("/api/orgs/{slug}/trusted-organizations", {
      ...path,
      body: { counterparty_org_id: henderson, sharing_description: "" },
    });
    expect(again.response.status).toBe(409);
    const self = await api.POST("/api/orgs/{slug}/trusted-organizations", {
      ...path,
      body: { counterparty_org_id: orgs.northlake.id, sharing_description: "" },
    });
    expect(self.response.status).toBe(422);

    const updated = await api.PUT("/api/orgs/{slug}/trusted-organizations/{counterparty_org_id}", {
      ...rowPath(vantage),
      body: { version: 1, autonomy: null },
    });
    expect(updated.data).toMatchObject({ autonomy: null, sharing_description: "", version: 2 });
    const stale = await api.PUT("/api/orgs/{slug}/trusted-organizations/{counterparty_org_id}", {
      ...rowPath(vantage),
      body: { version: 1, sharing_description: "x" },
    });
    expect(stale.response.status).toBe(409);

    const removed = await api.DELETE("/api/orgs/{slug}/trusted-organizations/{counterparty_org_id}", {
      params: { ...rowPath(pacific).params, query: { version: 1 } },
    });
    expect(removed.response.status).toBe(204);
    const read = await api.GET("/api/orgs/{slug}/harmony", path);
    expect(read.data?.trusted_organizations.map((row) => row.counterparty.slug).sort()).toEqual(["henderson", "vantage"]);
  });

  it("shows a member their org read-only and refuses their writes", async () => {
    const api = createFixtureApi(customizeRoutes(nonAdmin(members.alex)));
    const own = { params: { path: { slug: "henderson" } } };
    const read = await api.GET("/api/orgs/{slug}/harmony", own);
    expect(read.data?.editable).toBe(false);
    const save = await api.PUT("/api/orgs/{slug}/harmony", { ...own, body: { policy_version: 4, autonomy: "ask" } });
    expect(save.response.status).toBe(403);
    const trust = await api.POST("/api/orgs/{slug}/trusted-organizations", {
      ...own,
      body: { counterparty_org_id: orgs.northlake.id, sharing_description: "" },
    });
    expect(trust.response.status).toBe(403);
  });
});
