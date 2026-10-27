/** Customize → Harmony's gallery sections (/__gallery), over fixtures/customize.ts. */
import { useSession } from "../app/queries";
import { customizeRoutes, type CustomizeFixtureOptions } from "../fixtures/customize";
import { members, orgs } from "../fixtures/shell";
import type { MemberView } from "../api/types";
import type { GallerySection } from "../gallery/types";
import { ShellFrame } from "../shell/AppLayout";
import { HarmonySettingsScreen } from "./HarmonySettingsPage";
import type { HarmonySection } from "./types";

function Screen() {
  const session = useSession();
  return (
    // The real route collapses the sidebar through its store; the gallery pins it instead.
    <ShellFrame session={session.data ?? null} sidebar={{ collapsed: true }}>
      <HarmonySettingsScreen />
    </ShellFrame>
  );
}

function page(
  title: string,
  description: string,
  section: HarmonySection,
  actingAs: MemberView = members.helen,
  options: CustomizeFixtureOptions & { query?: string } = {},
): GallerySection {
  const { query = "", ...fixtureOptions } = options;
  return {
    title,
    description,
    layout: "page",
    height: 860,
    url: `/customize/harmony?section=${section}${query}`,
    fixtures: customizeRoutes(actingAs, fixtureOptions),
    render: () => <Screen />,
  };
}

export default [
  page(
    "Harmony settings: public profile",
    "Helen, an admin, editing Northlake's summary: one piece of prose other organizations and the network's search read.",
    "profile",
  ),
  page("Harmony settings: instructions", "The representative's private prompt, in prose.", "instructions"),
  page(
    "Harmony settings: sharing",
    "What Northlake's representative may pass on to everyone, and two trusted organizations: Pacific Beamline gets the full calendar, Vantage is on Ask.",
    "sharing",
  ),
  page(
    "Harmony settings: sharing, from a request",
    "Opened from \"Edit what you share with Henderson Lab\" in the Requests inbox: Henderson isn't trusted yet, so the pane suggests adding them.",
    "sharing",
    members.helen,
    { query: `&org=${orgs.henderson.id}` },
  ),
  page(
    "Harmony settings: autonomy",
    "The default mode with what each one means, the trusted organizations that have their own, and whether requests wait for a member's review.",
    "autonomy",
  ),
  page(
    "Harmony settings: send without review",
    "Northlake sends requests as soon as Claude writes them: the pane warns while that's the default.",
    "autonomy",
    members.helen,
    { patch: { send_without_confirmation: true } },
  ),
  page(
    "Harmony settings: send without review, view only",
    "Raj isn't an admin of Northlake: he sees the default and that he can change it for one chat, but not the setting itself.",
    "autonomy",
    members.raj,
    { patch: { send_without_confirmation: true } },
  ),
  page(
    "Harmony settings: skip",
    "With Skip as the default, the pane says answers go out without a sharing check.",
    "autonomy",
    members.helen,
    { patch: { autonomy: "skip" } },
  ),
  page(
    "Harmony settings: team and routing",
    "The routing prompt, the default assignee and each member's role and profile.",
    "team",
  ),
  page(
    "Harmony settings: view only",
    "Alex isn't an admin of Henderson Lab, so every pane is read-only.",
    "profile",
    members.alex,
  ),
  page(
    "Harmony settings: save conflict",
    "Change what you share and save: another admin's save landed first (409, with their own description). Their version reloads under your edits, and the bar names the field you both changed.",
    "sharing",
    members.helen,
    { conflict: true, latencyMs: 400 },
  ),
] satisfies GallerySection[];
