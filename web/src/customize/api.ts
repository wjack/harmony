/**
 * Customize → Harmony's reads and writes over the typed API client: the org's settings,
 * the save (the org's own fields, then each trusted organization that changed), and a
 * live refresh when the event stream says they changed.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { ApiError, unwrap, type ApiClient } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import { useApi, useDispatcher } from "../app/context";
import type { TrustedChange } from "./draft";
import type { HarmonySettingsUpdate, HarmonySettingsView, TrustedOrganizationView } from "./types";

export function useHarmonySettings(orgSlug: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.harmonySettings(orgSlug ?? ""),
    queryFn: () => unwrap(api.GET("/api/orgs/{slug}/harmony", { params: { path: { slug: orgSlug ?? "" } } })),
    enabled: orgSlug !== null,
  });
}

/** One trusted-organization write; null when the row was removed. */
async function writeTrusted(
  api: ApiClient,
  slug: string,
  change: TrustedChange,
): Promise<TrustedOrganizationView | null> {
  const path = { slug, counterparty_org_id: change.orgId };
  switch (change.kind) {
    case "add":
      return unwrap(api.POST("/api/orgs/{slug}/trusted-organizations", { params: { path: { slug } }, body: change.body }));
    case "update":
      return unwrap(
        api.PUT("/api/orgs/{slug}/trusted-organizations/{counterparty_org_id}", { params: { path }, body: change.body }),
      );
    case "remove": {
      // A 204 has no body for `unwrap` to return.
      const { error, response } = await api.DELETE("/api/orgs/{slug}/trusted-organizations/{counterparty_org_id}", {
        params: { path, query: { version: change.version } },
      });
      if (!response.ok) throw ApiError.from(response, error);
      return null;
    }
  }
}

function withTrustedRow(view: HarmonySettingsView, orgId: string, row: TrustedOrganizationView | null): HarmonySettingsView {
  const rest = view.trusted_organizations.filter((current) => current.counterparty.id !== orgId);
  return { ...view, trusted_organizations: row === null ? rest : [...rest, row] };
}

/** What one Save sends: the `PUT` for the org's own fields, then the trusted-organization writes. */
export interface SettingsSave {
  update: HarmonySettingsUpdate | null;
  changes: readonly TrustedChange[];
}

/** A save that stopped at a failed write; the writes before it landed, as `saved` shows. */
export class SaveFailed extends Error {
  readonly reason: unknown;
  readonly saved: HarmonySettingsView;

  constructor(reason: unknown, saved: HarmonySettingsView) {
    super(reason instanceof Error ? reason.message : "save failed");
    this.name = "SaveFailed";
    this.reason = reason;
    this.saved = saved;
  }
}

/** The write that failed, unwrapped from a save that stopped partway. */
function failure(error: unknown): unknown {
  return error instanceof SaveFailed ? error.reason : error;
}

/**
 * Saves the settings one write at a time, stopping at the first failure. Every response
 * goes into the cache as it lands, so the view always shows what the server holds.
 */
export function useSaveSettings(view: HarmonySettingsView) {
  const api = useApi();
  const queryClient = useQueryClient();
  const slug = view.org.slug;
  const key = queryKeys.harmonySettings(slug);
  return useMutation({
    mutationFn: async ({ update, changes }: SettingsSave): Promise<HarmonySettingsView> => {
      let saved = view;
      try {
        if (update !== null) {
          saved = await unwrap(api.PUT("/api/orgs/{slug}/harmony", { params: { path: { slug } }, body: update }));
          queryClient.setQueryData(key, saved);
        }
        for (const change of changes) {
          saved = withTrustedRow(saved, change.orgId, await writeTrusted(api, slug, change));
          queryClient.setQueryData(key, saved);
        }
      } catch (error) {
        throw new SaveFailed(error, saved);
      }
      return saved;
    },
    onError: (error) => {
      // A stale version or a lost admin role: show the settings as they now stand.
      if (isStaleSave(error) || isForbidden(error)) void queryClient.invalidateQueries({ queryKey: key });
    },
  });
}

/**
 * What a settings view is built from, as the org's stream carries it: another admin's
 * save (`rep_settings`, `rep_policy`, and `org` for the new settings version), a
 * trusted organization's row, and the members' profiles.
 */
export const SETTINGS_ENTITIES: ReadonlySet<string> = new Set([
  "org",
  "rep_settings",
  "rep_policy",
  "trusted_organization",
  "member",
]);

/** Refetch the settings when the tab's event stream carries a change to them. */
export function useSettingsLive(orgSlug: string | null) {
  const dispatcher = useDispatcher();
  const queryClient = useQueryClient();
  useEffect(() => {
    if (orgSlug === null) return;
    return dispatcher.onEnvelope((envelope) => {
      const { event } = envelope;
      if (event.type === "entity.snapshot" && SETTINGS_ENTITIES.has(event.entity_type)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.harmonySettings(orgSlug) });
      }
    });
  }, [dispatcher, queryClient, orgSlug]);
}

/** Whether a failed save was refused for its role: its edits can never be saved. */
export function isForbidden(error: unknown): boolean {
  const reason = failure(error);
  return reason instanceof ApiError && reason.status === 403;
}

/**
 * A save refused because another admin's save landed first (409). Its view is reloaded
 * and the edits replay on top of it, so this is a notice rather than a failure.
 */
export function isStaleSave(error: unknown): boolean {
  const reason = failure(error);
  return reason instanceof ApiError && reason.status === 409;
}

/** A failed save, as a line the save bar can show. */
export function saveErrorMessage(error: unknown): string {
  const reason = failure(error);
  if (reason instanceof ApiError) {
    if (reason.status === 403) return "Only admins can change Harmony settings.";
    if (reason.problem !== null) return reason.problem.detail;
  }
  return "Couldn't save. Check your connection and try again.";
}
