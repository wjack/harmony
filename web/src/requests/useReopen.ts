/**
 * The Reopen prompt's state (REOPEN.md §8.2): where it's open (under the Closed row, or
 * under a failed attempt's row for Try again), what the member typed, and its
 * `client_key`. The key is minted when the prompt opens and kept until a 202 or Cancel,
 * across 409s and timeouts: a 409 writes nothing, so resending under it is safe, and
 * after no answer it makes the retry the same write. A later prompt mints a new one.
 *
 * When the conversation turns out to be open already, the prompt closes and its text
 * goes to the box's Edit (`toEdit`), with why.
 */
import { useState } from "react";

import { useSession } from "../app/queries";
import { problemOf, useReopenThread } from "./api";
import { reopenError, type ReopenError } from "./failure";

export interface ReopenPrompt {
  /** The transcript entry the prompt opens under. */
  anchor: string;
  key: string;
  text: string;
}

export interface Reopen {
  prompt: ReopenPrompt | null;
  open: (anchor: string, text?: string) => void;
  cancel: () => void;
  setText: (text: string) => void;
  submit: () => void;
  pending: boolean;
  error: ReopenError | null;
}

export function useReopen(
  threadId: string,
  names: { us: string; them: string },
  toEdit: (text: string, why: string) => void,
): Reopen {
  const reopen = useReopenThread(threadId);
  const viewerId = useSession().data?.member.id;
  const [prompt, setPrompt] = useState<ReopenPrompt | null>(null);
  const [error, setError] = useState<ReopenError | null>(null);

  const close = () => {
    setPrompt(null);
    setError(null);
    reopen.reset();
  };

  return {
    prompt,
    open: (anchor, text = "") => {
      setError(null);
      reopen.reset();
      setPrompt({ anchor, key: crypto.randomUUID(), text });
    },
    cancel: close,
    setText: (text) => {
      setPrompt((current) => (current === null ? null : { ...current, text }));
    },
    submit: () => {
      const body = prompt?.text.trim() ?? "";
      if (prompt === null || body === "" || reopen.isPending) return;
      setError(null);
      reopen.mutate(
        { client_key: prompt.key, body },
        {
          onSuccess: close,
          onError: (failure) => {
            const shown = reopenError(failure, problemOf(failure), viewerId, names);
            if (!shown.toEdit) {
              setError(shown);
              return;
            }
            close();
            toEdit(prompt.text, shown.text);
          },
        },
      );
    },
    pending: reopen.isPending,
    error,
  };
}
