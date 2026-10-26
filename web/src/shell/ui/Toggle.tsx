import { Switch } from "radix-ui";

import { cx } from "./cx";

interface ToggleProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  /** Accessible name when no visible `<label>` points at it. */
  label?: string;
  id?: string;
  disabled?: boolean;
}

/** The settings toggle: blue when on (UI_REFERENCE.md §1, `--toggle-on`). */
export function Toggle({ checked, onCheckedChange, label, id, disabled }: ToggleProps) {
  return (
    <Switch.Root
      id={id}
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      aria-label={label}
      className={cx(
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border border-transparent transition-colors duration-150 disabled:opacity-50",
        checked ? "bg-toggle" : "bg-border-strong",
      )}
    >
      <Switch.Thumb className="block size-4 translate-x-0.5 rounded-full bg-white shadow-[0_1px_2px_rgb(20_20_19/0.25)] transition-transform duration-150 data-[state=checked]:translate-x-[17px]" />
    </Switch.Root>
  );
}
