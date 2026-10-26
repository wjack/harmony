/**
 * Customize → Harmony's named types, derived from the generated OpenAPI schema
 * (`GET`/`PUT /api/orgs/{slug}/harmony` and `/api/orgs/{slug}/trusted-organizations`).
 */
import type { components } from "../api/schema";

type Schemas = components["schemas"];

export type HarmonySettingsView = Schemas["HarmonySettingsView"];
export type HarmonySettingsUpdate = Schemas["HarmonySettingsUpdate"];
export type TrustedOrganizationView = Schemas["TrustedOrganizationView"];
export type TrustedOrganizationCreate = Schemas["TrustedOrganizationCreate"];
export type TrustedOrganizationUpdate = Schemas["TrustedOrganizationUpdate"];
export type MemberProfileView = Schemas["MemberProfileView"];
export type OrgRef = Schemas["OrgRef"];
export type MemberRef = Schemas["MemberRef"];

export type Autonomy = HarmonySettingsView["autonomy"];
export type DirectoryVisibility = HarmonySettingsView["directory_visibility"];

/** The panes of Customize → Harmony (REVISION_UNSTRUCTURED.md §9), keyed as `?section=`. */
export type HarmonySection = "profile" | "instructions" | "sharing" | "autonomy" | "team";
