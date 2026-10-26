import { useMemo } from "react";

import { useMembers } from "../app/queries";
import type { HarmonySettingsView, OrgRef } from "./types";

/**
 * Every organization a trusted row can name, by id: the member directory's orgs other
 * than our own, plus any already trusted (the directory may not list them all).
 */
export function useCounterparties(view: HarmonySettingsView): ReadonlyMap<string, OrgRef> {
  const directory = useMembers();
  return useMemo(() => {
    const byId = new Map<string, OrgRef>();
    for (const row of view.trusted_organizations) byId.set(row.counterparty.id, row.counterparty);
    for (const member of [...(directory.data?.featured ?? []), ...(directory.data?.all ?? [])]) {
      if (member.org.id !== view.org.id) byId.set(member.org.id, member.org);
    }
    return byId;
  }, [directory.data, view.org.id, view.trusted_organizations]);
}

export function byName(a: OrgRef, b: OrgRef): number {
  return a.name.localeCompare(b.name);
}
