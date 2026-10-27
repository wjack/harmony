import type { ButtonHTMLAttributes, Ref } from "react";

import { cx } from "../shell/ui";

/**
 * A button whose action goes to our own Claude and nowhere else: Tell Claude, Reopen.
 * Blue, as Edit is (black and white is what can reach the other org; amber is the team).
 */
export function ClaudeButton({
  className,
  type = "button",
  ref,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { ref?: Ref<HTMLButtonElement> }) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx(
        "inline-flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-control bg-info px-3 text-ui font-medium whitespace-nowrap text-white transition-[filter,opacity] duration-100 select-none hover:brightness-95 disabled:opacity-40 disabled:hover:brightness-100",
        className,
      )}
      {...rest}
    />
  );
}
