/**
 * Customize → Harmony fixtures: Northlake's and Henderson Lab's settings from the
 * harmony-demo scenario (docs/SCENARIO.md), with stateful writes that behave as the
 * API's do (src/harmony/api/routes/orgs.py, docs/P1_P2_CONTRACT.md):
 * - admins only (403 `not_admin`);
 * - `PUT /harmony`: a stale `policy_version` is 409 `invalid`, and only a save that
 *   changes a policy field moves that version, so prompt- and profile-only saves never
 *   conflict; the public summary and instructions write a new `settings_version` only
 *   when they actually changed;
 * - trusted organizations: another org of the world, once (409 `invalid` if already
 *   trusted, 422 `invalid` for your own org); each row has its own version.
 */
import type { components } from "../api/schema";
import type { MemberView } from "../api/types";
import { problem, type FixtureRequest, type FixtureRoutes } from "./index";
import { FIXTURE_NOW, members, orgs, shellRoutes } from "./shell";

type HarmonySettingsView = components["schemas"]["HarmonySettingsView"];
type HarmonySettingsUpdate = components["schemas"]["HarmonySettingsUpdate"];
type TrustedOrganizationView = components["schemas"]["TrustedOrganizationView"];
type TrustedOrganizationCreate = components["schemas"]["TrustedOrganizationCreate"];
type TrustedOrganizationUpdate = components["schemas"]["TrustedOrganizationUpdate"];
type OrgRef = components["schemas"]["OrgRef"];

function daysAgo(days: number): string {
  return new Date(Date.parse(FIXTURE_NOW) - days * 86_400_000).toISOString();
}

function ref(member: MemberView) {
  return { id: member.id, slug: member.slug, name: member.name };
}

function trusted(
  counterparty: OrgRef,
  autonomy: TrustedOrganizationView["autonomy"],
  sharing: string,
  by: MemberView,
  days: number,
): TrustedOrganizationView {
  return { counterparty, autonomy, sharing_description: sharing, updated_by: ref(by), updated_at: daysAgo(days), version: 1 };
}

export const northlakeSettings: HarmonySettingsView = {
  org: orgs.northlake,
  settings_version: 3,
  public_summary:
    "Northlake Characterization Core is the shared-instrument facility at Northlake University, open to external academic and industry users. " +
    "We run powder XRD (Bruker D8, with air-free dome holders), SEM with EDS, and XPS.\n\n" +
    "Ask us for quotes, scheduling and sample-prep requirements. We don't do synthesis, sample shipping, or data interpretation beyond our standard reports. " +
    "Typical turnaround is 5–10 business days; priority by arrangement.",
  instructions:
    "Represent the Northlake Characterization Core to outside labs and companies. Be friendly, precise and brief.\n\n" +
    "Quote from the external academic price list. Never quote below it, and never mention another client by name.\n\n" +
    "For priority requests, reserve slots or pricing exceptions, check with the team before you commit: Helen decides those. " +
    "For sample-prep and air-free handling questions, Raj is the expert.\n\n" +
    "If a request is outside our services (synthesis, shipping, interpretation), decline politely and say what we can do instead.",
  sharing_description:
    "Our external academic price list, what is open on the public instrument calendar, sample-prep and air-free handling requirements, and typical turnaround. " +
    "Not: other clients or their bookings, internal reserve slots, or discounts.",
  autonomy: "auto",
  trusted_organizations: [
    trusted(
      orgs.pacific,
      null,
      "Our full XRD calendar, including internal reserve blocks, so we can line up beamtime with lab time.",
      members.helen,
      12,
    ),
    trusted(orgs.vantage, "ask", "", members.helen, 30),
  ],
  directory_visibility: "public",
  routing_prompt:
    "Instrument scheduling, priority or reserve-slot requests, and pricing exceptions go to Helen. " +
    "Sample-prep and air-free handling questions go to Raj.",
  default_assignee: ref(members.helen),
  members: [
    {
      member: ref(members.helen),
      role: "admin",
      title: "Core director",
      profile: "Runs scheduling, priority requests, pricing exceptions and external contracts.",
    },
    {
      member: ref(members.raj),
      role: "member",
      title: "Staff scientist, XRD and SEM",
      profile: "Runs the instruments. Sample prep and air-free handling questions.",
    },
  ],
  policy_version: 7,
  editable: true,
};

export const hendersonSettings: HarmonySettingsView = {
  org: orgs.henderson,
  settings_version: 2,
  public_summary:
    "Henderson Lab, Dept. of Materials Science, Northlake University. Battery materials research: sodium-ion cathodes from Earth-abundant elements. " +
    "We can run coin-cell cycling for collaborators on request.",
  instructions:
    "We mostly request services: characterization, fabrication and compute. Keep sample compositions and budgets private. " +
    "Ask Alex about samples and logistics; anything about spending goes to Maya.",
  sharing_description:
    "Sample counts, sample handling needs (air-sensitive, glovebox), deadlines and the lab's contact details. Not: compositions, dopants or budgets.",
  autonomy: "auto",
  trusted_organizations: [],
  directory_visibility: "communities",
  routing_prompt: "Questions about samples and logistics go to Alex. Spending, contracts and collaborations go to Maya.",
  default_assignee: ref(members.maya),
  members: [
    {
      member: ref(members.alex),
      role: "member",
      title: "PhD student",
      profile: "Runs the Na-ion cathode synthesis and owns sample logistics.",
    },
    {
      member: ref(members.maya),
      role: "admin",
      title: "Principal investigator",
      profile: "Approves spending and external collaborations.",
    },
  ],
  policy_version: 4,
  editable: false,
};

const POLICY_FIELDS = [
  "sharing_description",
  "autonomy",
  "directory_visibility",
  "routing_prompt",
  "default_assignee_id",
] as const;

/** The view after `update`, as the API would return it. */
function applyUpdate(view: HarmonySettingsView, update: HarmonySettingsUpdate): HarmonySettingsView {
  const assignee =
    update.default_assignee_id === undefined
      ? view.default_assignee
      : (view.members.find((member) => member.member.id === update.default_assignee_id)?.member ?? null);
  const next: HarmonySettingsView = {
    ...view,
    public_summary: update.public_summary ?? view.public_summary,
    instructions: update.instructions ?? view.instructions,
    sharing_description: update.sharing_description ?? view.sharing_description,
    autonomy: update.autonomy ?? view.autonomy,
    directory_visibility: update.directory_visibility ?? view.directory_visibility,
    routing_prompt: update.routing_prompt ?? view.routing_prompt,
    default_assignee: assignee,
    members: view.members.map((member) => ({
      ...member,
      profile: update.member_profiles?.[member.member.id] ?? member.profile,
    })),
  };
  const prompt = (settings: HarmonySettingsView) => [settings.public_summary, settings.instructions].join("\0");
  return {
    ...next,
    settings_version: prompt(next) === prompt(view) ? view.settings_version : view.settings_version + 1,
    policy_version: POLICY_FIELDS.some((field) => field in update) ? view.policy_version + 1 : view.policy_version,
  };
}

/** Every org of the fixture world, by id: what a trusted row may name. */
const WORLD_ORGS = new Map<string, OrgRef>(Object.values(orgs).map((org) => [org.id, org]));

export interface CustomizeFixtureOptions {
  /**
   * The first save meets another admin's save that landed first: a new description of
   * what you share (a policy change, so the save is 409).
   */
  conflict?: boolean;
  /** Simulated latency on reads and saves, in ms. */
  latencyMs?: number;
  /** Changes to the starting settings (both orgs), e.g. `{ autonomy: "skip" }`. */
  patch?: Partial<Omit<HarmonySettingsView, "org" | "editable">>;
}

/** Their save, in the `conflict` scenario. */
export const THEIR_SHARING =
  "Our external academic price list, the public instrument calendar, sample-prep requirements and typical turnaround. Not: other clients, reserve slots or discounts.";

/**
 * Shell routes acting as `actingAs`, plus the Harmony settings and trusted-organization
 * routes over Northlake's and Henderson Lab's settings. Only the actor's own org is
 * visible, and only an admin of it may write.
 */
export function customizeRoutes(actingAs: MemberView, options: CustomizeFixtureOptions = {}): FixtureRoutes {
  const views = new Map<string, HarmonySettingsView>(
    [northlakeSettings, hendersonSettings].map((view) => [
      view.org.slug,
      { ...view, ...options.patch, editable: actingAs.role === "admin" && actingAs.org.slug === view.org.slug },
    ]),
  );
  let conflict = options.conflict ?? false;
  const wait = () => new Promise((resolve) => setTimeout(resolve, options.latencyMs ?? 0));

  /** The actor's own org's view, or the error response for anyone else. */
  const own = (slug: string | undefined, write: boolean): HarmonySettingsView | Response => {
    const view = views.get(slug ?? "");
    if (view === undefined || slug !== actingAs.org.slug) return problem(404, "not_found", "no such org");
    if (write && !view.editable) return problem(403, "not_admin", "only an admin can change these settings");
    return view;
  };
  const rowOf = (view: HarmonySettingsView, orgId: string | undefined) =>
    view.trusted_organizations.find((row) => row.counterparty.id === orgId);
  const withRows = (view: HarmonySettingsView, rows: TrustedOrganizationView[]) => {
    views.set(view.org.slug, { ...view, trusted_organizations: rows });
  };

  return {
    ...shellRoutes(actingAs),
    "GET /api/orgs/{slug}/harmony": async ({ params }) => {
      await wait();
      return own(params.slug, false);
    },
    "PUT /api/orgs/{slug}/harmony": async ({ params, body }) => {
      await wait();
      const view = own(params.slug, true);
      if (view instanceof Response) return view;
      const update = body as HarmonySettingsUpdate;
      if (conflict) {
        conflict = false;
        views.set(view.org.slug, applyUpdate(view, { policy_version: view.policy_version, sharing_description: THEIR_SHARING }));
      }
      const current = views.get(view.org.slug) ?? view;
      if (update.policy_version !== current.policy_version) {
        return problem(409, "invalid", `these settings changed (version ${String(current.policy_version)}); reload and try again`);
      }
      const next = applyUpdate(current, update);
      views.set(view.org.slug, next);
      return next;
    },
    "POST /api/orgs/{slug}/trusted-organizations": async ({ params, body }) => {
      await wait();
      const view = own(params.slug, true);
      if (view instanceof Response) return view;
      const create = body as TrustedOrganizationCreate;
      const counterparty = WORLD_ORGS.get(create.counterparty_org_id);
      if (counterparty === undefined || counterparty.id === view.org.id) {
        return problem(422, "invalid", "a trusted organization must be another organization of this world");
      }
      if (rowOf(view, counterparty.id) !== undefined) {
        return problem(409, "invalid", `${counterparty.name} is already a trusted organization`);
      }
      const row: TrustedOrganizationView = {
        counterparty,
        autonomy: create.autonomy ?? null,
        sharing_description: create.sharing_description,
        updated_by: ref(actingAs),
        updated_at: FIXTURE_NOW,
        version: 1,
      };
      withRows(view, [...view.trusted_organizations, row]);
      return row;
    },
    "PUT /api/orgs/{slug}/trusted-organizations/{counterparty_org_id}": async ({ params, body }) => {
      await wait();
      const view = own(params.slug, true);
      if (view instanceof Response) return view;
      const row = rowOf(view, params.counterparty_org_id);
      if (row === undefined) return problem(404, "not_found", "not a trusted organization");
      const update = body as TrustedOrganizationUpdate;
      if (update.version !== row.version) {
        return problem(409, "invalid", `this organization's settings changed (version ${String(row.version)}); reload and try again`);
      }
      const next: TrustedOrganizationView = {
        ...row,
        autonomy: "autonomy" in update ? (update.autonomy ?? null) : row.autonomy,
        sharing_description: update.sharing_description ?? row.sharing_description,
        updated_by: ref(actingAs),
        updated_at: FIXTURE_NOW,
        version: row.version + 1,
      };
      withRows(
        view,
        view.trusted_organizations.map((current) => (current === row ? next : current)),
      );
      return next;
    },
    // A 204 route has no body type, so its handler names its request type itself.
    "DELETE /api/orgs/{slug}/trusted-organizations/{counterparty_org_id}": async ({ params, query }: FixtureRequest) => {
      await wait();
      const view = own(params.slug, true);
      if (view instanceof Response) return view;
      const row = rowOf(view, params.counterparty_org_id);
      if (row === undefined) return problem(404, "not_found", "not a trusted organization");
      if (query.get("version") !== String(row.version)) {
        return problem(409, "invalid", `this organization's settings changed (version ${String(row.version)}); reload and try again`);
      }
      withRows(
        view,
        view.trusted_organizations.filter((current) => current !== row),
      );
      return new Response(null, { status: 204 });
    },
  };
}
