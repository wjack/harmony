/**
 * The network view's named types, derived from the generated OpenAPI schema
 * (`GET /api/network`), so a regenerated `schema.d.ts` flows through without edits.
 */
import type { components } from "../api/schema";

type Schemas = components["schemas"];

export type NetworkView = Schemas["NetworkView"];
export type NetworkNode = Schemas["NetworkNode"];
export type NetworkEdge = Schemas["NetworkEdge"];
export type NetworkCounts = Schemas["NetworkCounts"];
export type NetworkRequestRef = Schemas["NetworkRequestRef"];
export type WaitingOn = Schemas["WaitingOn"];
export type OrgRef = Schemas["OrgRef"];
export type MemberRef = Schemas["MemberRef"];

export type ParticipantStatus = NonNullable<NetworkNode["participant"]>;
export type Fit = NonNullable<NetworkNode["fit"]>;
export type NodeRole = NetworkNode["role"];
export type ThreadState = NetworkEdge["state"];
export type RequestStatus = NetworkRequestRef["status"];

/** Which of the session's two worlds the lens shows (ARCHITECTURE.md §11.1). */
export type WorldLens = "desk" | "network";
