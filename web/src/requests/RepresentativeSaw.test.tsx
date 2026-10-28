// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { requestsRoutes, requestsWorld } from "../fixtures/requests";
import { orgs } from "../fixtures/shell";
import { FixtureProviders } from "../gallery/FixtureProviders";
import { RepresentativeSaw } from "./RepresentativeSaw";
import type { RepresentativeContext } from "./types";

afterEach(cleanup);

const FROZEN = "Our external academic price list. Not: staff notes or off-list pricing.";
const SHARING_NOTICE =
  "Notice from Harmony: your organization changed what you may pass on to this organization. " +
  "It now reads as follows, in place of what your prompt says:\n\nOpen SEM slots on weekdays too.";

const SAW: RepresentativeContext = {
  parts: [{ kind: "sharing", label: "What it may pass on", text: FROZEN }],
  notices: [{ kind: "sharing", text: SHARING_NOTICE, delivered_at: "2026-11-02T10:05:00Z" }],
  released: [],
  guidance: [],
  frozen_at: "2026-11-02T09:00:00Z",
};

function show(representative: RepresentativeContext): HTMLElement {
  cleanup();
  render(
    <FixtureProviders routes={requestsRoutes({ world: requestsWorld() })}>
      <RepresentativeSaw
        open
        onOpenChange={() => undefined}
        returnFocus={() => undefined}
        us={orgs.northlake}
        them={orgs.henderson}
        representative={representative}
      />
    </FixtureProviders>,
  );
  return screen.getByRole("dialog", { name: "What our representative saw" });
}

it("lists a sharing notice sent after the prompt froze, beside the part it replaced", () => {
  const drawer = show(SAW);
  expect(within(drawer).getByRole("region", { name: "Its prompt" }).textContent).toContain(FROZEN);
  const notices = within(drawer).getByRole("region", { name: "Notices since" });
  expect(within(notices).getByText("What it may pass on changed")).toBeTruthy();
  expect(notices.textContent).toContain("Open SEM slots on weekdays too.");
});

it("says plainly when nothing was sent since", () => {
  const drawer = show({ ...SAW, notices: [] });
  const notices = within(drawer).getByRole("region", { name: "Notices since" });
  expect(notices.textContent).toContain("None yet.");
  // The footer no longer claims the list covers what the other side wrote.
  expect(drawer.textContent).toContain(`Besides what ${orgs.henderson.name} wrote on this thread`);
});

it("lists each version of guidance our side's edits sent it, oldest first", () => {
  const drawer = show({
    ...SAW,
    guidance: [
      { version: 2, text: "Say only whether a slot is available. Offer the general capabilities sheet.", delivered_at: "2026-11-02T11:00:00Z" },
      { version: 1, text: "Say only whether a slot is available.", delivered_at: "2026-11-02T10:30:00Z" },
    ],
  });
  const guidance = within(drawer).getByRole("region", { name: "Guidance from your side" });
  expect(within(guidance).getAllByRole("heading", { level: 4 }).map((heading) => heading.textContent)).toEqual([
    "Guidance, version 1",
    "Guidance, version 2",
  ]);
  expect(guidance.textContent).toContain("Offer the general capabilities sheet.");
  expect(within(show(SAW)).getByRole("region", { name: "Guidance from your side" }).textContent).toContain("None yet.");
});
