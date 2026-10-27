/**
 * The request view's box below the transcript, as state the view shares: its mode
 * (Comment for the team, or Edit to tell our Claude what to do differently) and its
 * text. The decision card reads it to say an instruction isn't sent yet, and the rows
 * above can put text back into Edit: an Edit's Try again, or a reopen that found the
 * conversation open already.
 */
import { useState } from "react";

export type ComposerMode = "comment" | "edit";

export interface Composer {
  mode: ComposerMode;
  text: string;
  setMode: (mode: ComposerMode) => void;
  setText: (text: string) => void;
  /** Puts `text` in Edit and asks the field for focus, with why it's there when that isn't obvious. */
  toEdit: (text: string, note?: string) => void;
  /** Why the text arrived in Edit, until the member sends it or switches mode. */
  note: string | null;
  /** Changes whenever something outside the box asks its field for focus. */
  focusRequest: number;
}

export function useComposer(): Composer {
  const [mode, setModeState] = useState<ComposerMode>("comment");
  const [text, setText] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [focusRequest, setFocusRequest] = useState(0);
  return {
    mode,
    text,
    setMode: (next) => {
      setModeState(next);
      setNote(null);
    },
    setText,
    toEdit: (next, why) => {
      setModeState("edit");
      setText(next);
      setNote(why ?? null);
      setFocusRequest((count) => count + 1);
    },
    note,
    focusRequest,
  };
}
