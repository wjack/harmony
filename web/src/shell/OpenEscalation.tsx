import type { ReactNode } from "react";
import { Navigate, useSearchParams } from "react-router";

import { useInbox } from "../app/queries";
import { escalationPath, itemForEscalation } from "./escalationPlace";

/**
 * `/requests?escalation=<id>`, where a needs-human chip or toast sends you (§11.4: one
 * click acts as the assignee "and opens the item"). Once the acting member's inbox has
 * the item, this opens it where it is answered. Until then, or if it isn't theirs
 * (already resolved, someone else's), the inbox shows as usual.
 */
export function OpenEscalation({ children }: { children: ReactNode }) {
  const [params] = useSearchParams();
  const escalationId = params.get("escalation");
  if (escalationId === null) return children;
  return <EscalationRedirect escalationId={escalationId}>{children}</EscalationRedirect>;
}

function EscalationRedirect({ escalationId, children }: { escalationId: string; children: ReactNode }) {
  const inbox = useInbox();
  const item = inbox.data === undefined ? null : itemForEscalation(inbox.data, escalationId);
  if (item === null) return children;
  return <Navigate to={escalationPath(item)} replace />;
}
