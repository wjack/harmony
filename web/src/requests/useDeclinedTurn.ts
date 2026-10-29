/**
 * An open `refused_turn` item's actions (a turn Claude declined): Retry runs Claude's
 * turn again (`approve`), and ending the conversation closes it (`decline`). Shared by
 * its card and the box, whose sent Edit retries the turn.
 */
import { useState } from "react";

import { budgetLine, useBudgetState } from "../chat/failureCopy";
import { problemOf, useResolveEscalation } from "./api";
import type { EscalationView } from "./types";

export interface DeclinedTurn {
  escalation: EscalationView;
  /** Run Claude's turn again. */
  retry: () => void;
  /** Close the conversation, as Claude's refusal would have. */
  end: () => void;
  /** A Retry or an end is on its way. */
  busy: boolean;
  /** Why live turns can't start now (`budgetLine`): Retry waits until it clears. */
  blocked: string | null;
  /** Why the last call didn't go through. */
  notice: string | null;
}

export function useDeclinedTurn(escalation: EscalationView | null, threadId: string): DeclinedTurn | null {
  const resolve = useResolveEscalation({ threadId, escalationId: escalation?.id ?? "" });
  const blocked = budgetLine(useBudgetState());
  const [notice, setNotice] = useState<string | null>(null);
  if (escalation === null) return null;
  const send = (action: "approve" | "decline") => {
    if (resolve.isPending) return;
    setNotice(null);
    resolve.mutate(
      { action },
      {
        onError: (error) => {
          const problem = problemOf(error);
          if (problem?.code === "escalation_not_open") {
            setNotice(
              problem.resolved_by == null
                ? "This changed while you were deciding. Check the latest before trying again."
                : `Already decided by ${problem.resolved_by.name}.`,
            );
            return;
          }
          setNotice(problem?.detail ?? error.message);
        },
      },
    );
  };
  return {
    escalation,
    retry: () => {
      if (blocked === null) send("approve");
    },
    end: () => {
      send("decline");
    },
    busy: resolve.isPending,
    blocked,
    notice,
  };
}
