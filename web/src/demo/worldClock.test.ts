import { describe, expect, it } from "vitest";

import type { WorldView } from "../api/types";
import { members } from "../fixtures/shell";
import { addNeedsHumanAlert, alertStore, alertsByAccount, clearAlertsFor } from "./alerts";
import { formatWorldDate, worldDate } from "./worldClock";

const world: WorldView = {
  id: "0b1d7a3e-5c2f-4e8a-9a51-000000000001",
  scenario: "harmony-demo",
  mode: "live",
  status: "live",
  created_at: "2026-03-02T15:00:00Z",
  ff_offset_s: 0,
  skip_approvals: false,
};

describe("worldDate", () => {
  const created = Date.parse(world.created_at);

  it("starts at the scenario's anchor whenever the world was made", () => {
    expect(formatWorldDate(worldDate(world, created))).toBe("Mon, Oct 26");
  });

  it("moves with real time and with fast-forward", () => {
    expect(formatWorldDate(worldDate(world, created + 25 * 3_600_000))).toBe("Tue, Oct 27");
    expect(formatWorldDate(worldDate({ ...world, ff_offset_s: 7 * 86_400 }, created))).toBe("Mon, Nov 2");
  });
});

describe("needs-human alerts", () => {
  const cue = (entityId: string, memberSlug: string, orgSlug = "northlake") => ({
    type: "demo.needs_human" as const,
    kind: "needs_human",
    org_slug: orgSlug,
    member_slug: memberSlug,
    entity_id: entityId,
  });

  it("keeps one alert per escalation and clears an account's alerts when acting as them", () => {
    alertStore.setState({ alerts: [] });
    addNeedsHumanAlert(cue("e1", members.helen.slug), 1);
    addNeedsHumanAlert(cue("e1", members.helen.slug), 2);
    addNeedsHumanAlert(cue("e2", members.dana.slug), 3);
    expect(alertStore.getState().alerts.map((alert) => alert.escalationId)).toEqual(["e2", "e1"]);

    expect(clearAlertsFor(members.helen.slug, "northlake").map((alert) => alert.escalationId)).toEqual(["e1"]);
    expect(alertStore.getState().alerts.map((alert) => alert.memberSlug)).toEqual([members.dana.slug]);
  });

  it("clears only the alerts of the member at that org (slugs are per org)", () => {
    alertStore.setState({ alerts: [] });
    addNeedsHumanAlert(cue("e4", "admin", "cascade"), 1);
    addNeedsHumanAlert(cue("e5", "admin", "pacific-beamline"), 2);

    expect(clearAlertsFor("admin", "cascade").map((alert) => alert.escalationId)).toEqual(["e4"]);
    expect(alertStore.getState().alerts.map((alert) => alert.escalationId)).toEqual(["e5"]);
  });

  it("groups alerts into one per account, newest account first", () => {
    alertStore.setState({ alerts: [] });
    addNeedsHumanAlert(cue("e1", members.helen.slug), 1);
    addNeedsHumanAlert(cue("e2", members.dana.slug), 2);
    addNeedsHumanAlert(cue("e3", members.helen.slug), 3);

    const accounts = alertsByAccount(alertStore.getState().alerts);
    expect(accounts.map((account) => [account.memberSlug, account.alerts.map((alert) => alert.escalationId)])).toEqual([
      [members.helen.slug, ["e3", "e1"]],
      [members.dana.slug, ["e2"]],
    ]);
  });

  it("ignores a cue without an assignee", () => {
    alertStore.setState({ alerts: [] });
    expect(addNeedsHumanAlert({ ...cue("e3", members.helen.slug), member_slug: null })).toBeNull();
    expect(alertStore.getState().alerts).toEqual([]);
  });
});
