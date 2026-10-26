/**
 * The Requests inbox's named types, all derived from the generated OpenAPI types
 * (`make gen-api`), so a regenerated `schema.d.ts` flows through without hand edits.
 */
import type { components } from "../api/schema";

type Schemas = components["schemas"];

// --- GET /api/inbox ------------------------------------------------------------------

export type Inbox = Schemas["Inbox"];
export type InboxItem = Schemas["InboxItem"];
export type InboxEscalation = Schemas["InboxEscalation"];
export type InboxSection = keyof Inbox;

// --- GET /api/threads/{id} (the shared transcript) -----------------------------------

export type ThreadDetail = Schemas["ThreadDetail"];
export type ThreadMessage = Schemas["ThreadMessageView"];
export type ThreadAccess = Schemas["ThreadAccessView"];
export type ThreadState = ThreadDetail["state"];
export type Side = ThreadDetail["side"];
export type Provenance = ThreadMessage["provenance"];

// --- GET /api/threads/{id}/workspace (our side only) ---------------------------------

export type ThreadWorkspace = Schemas["ThreadWorkspace"];
export type ConsultView = Schemas["ConsultView"];
export type EscalationView = Schemas["EscalationView"];
export type EscalationKind = EscalationView["kind"];
export type EscalationDraft = Schemas["EscalationDraftView"];
export type ActivityLine = Schemas["ActivityLine"];
export type NoteView = Schemas["NoteView"];
export type RepresentativeContext = Schemas["RepresentativeContext"];
export type ContextPart = Schemas["ContextPart"];
export type DeliveredNotice = Schemas["DeliveredNotice"];
export type ReleasedAnswer = Schemas["ReleasedAnswerView"];

// --- Mutations -------------------------------------------------------------------------

export type ResolveEscalation = Schemas["ResolveEscalation"];
export type EscalationAction = ResolveEscalation["action"];
export type EscalationResolved = Schemas["EscalationResolved"];
export type ReassignEscalation = Schemas["ReassignEscalation"];
export type Reassigned = Schemas["Reassigned"];
export type RedraftStarted = Schemas["RedraftStarted"];
export type AddNote = Schemas["AddNote"];
export type ShareRequest = Schemas["ShareRequest"];
export type ShareResult = Schemas["ShareResult"];

// --- Shared ----------------------------------------------------------------------------

export type OrgRef = Schemas["OrgRef"];
export type MemberRef = Schemas["MemberRef"];
export type MemberView = Schemas["MemberView"];
export type MemberDirectory = Schemas["MemberDirectory"];
export type ApiProblem = Schemas["ApiProblem"];
