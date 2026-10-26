import { cx } from "../shell/ui";

/**
 * Said wherever a person decides something the agents then act on. The demo runs only
 * on recorded model output (Demo controls → Model), played back by turn rather than by
 * what anyone wrote, so a different answer or choice can't change what Claude does
 * next. Saying so keeps that from looking like Claude ignoring the person.
 */
export function ScriptedNote({ className }: { className?: string }) {
  return (
    <p className={cx("text-[12px] leading-4 text-faint", className)}>
      Scripted demo: Claude replays the recorded story, so it carries on as the walkthrough says, whatever you write
      or choose here.
    </p>
  );
}
