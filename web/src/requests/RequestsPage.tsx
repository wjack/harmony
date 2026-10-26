/**
 * `/requests` and `/requests/:threadId` (ARCHITECTURE §12). The shell mounts this
 * inside its layout; the open thread lives in the URL.
 */
import { useCallback } from "react";
import { useNavigate, useParams } from "react-router";

import { RequestsInbox } from "./RequestsInbox";

export function RequestsPage() {
  const { threadId } = useParams<{ threadId?: string }>();
  const navigate = useNavigate();
  const open = useCallback(
    (id: string) => {
      void navigate(`/requests/${id}`);
    },
    [navigate],
  );
  const close = useCallback(() => {
    void navigate("/requests");
  }, [navigate]);

  return <RequestsInbox threadId={threadId} onOpenThread={open} onCloseThread={close} />;
}
