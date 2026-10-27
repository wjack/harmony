import type { ReactNode } from "react";

import { cx } from "../shell/ui";

export interface RadioOption<T extends string> {
  value: T;
  label: string;
  description: string;
  icon?: ReactNode;
  /** Beside the label: "Recommended". */
  tag?: ReactNode;
}

interface RadioListProps<T extends string> {
  name: string;
  /** Names the group for assistive tech: "Default mode". */
  label: string;
  value: T;
  options: readonly RadioOption<T>[];
  onChange: (value: T) => void;
  disabled: boolean;
}

/**
 * Radio rows in a bordered list box, the way Claude's modals list choices. Disabled, it
 * drops the white box and the black dot, so a viewer reads the choice without being
 * invited to change it.
 */
export function RadioList<T extends string>({ name, label, value, options, onChange, disabled }: RadioListProps<T>) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      aria-disabled={disabled || undefined}
      className={cx(
        "divide-y divide-border overflow-hidden rounded-menu border border-border",
        disabled ? "bg-transparent" : "bg-surface",
      )}
    >
      {options.map((option) => {
        const checked = option.value === value;
        return (
          <label
            key={option.value}
            className={cx(
              "flex items-start gap-3 px-4 py-3 transition-colors",
              disabled ? "cursor-default" : "cursor-pointer hover:bg-canvas",
            )}
          >
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={checked}
              disabled={disabled}
              onChange={() => {
                onChange(option.value);
              }}
              className="peer sr-only"
            />
            <span
              aria-hidden
              className={cx(
                "mt-0.5 size-4 shrink-0 rounded-full peer-focus-visible:ring-2 peer-focus-visible:ring-accent/40",
                checked
                  ? cx("flex items-center justify-center", disabled ? "bg-muted" : "bg-primary")
                  : cx("border", disabled ? "border-border" : "border-border-strong bg-surface"),
              )}
            >
              {checked && <span className="size-1.5 rounded-full bg-surface" />}
            </span>
            <span className="min-w-0 flex-1">
              <span
                className={cx(
                  "flex items-center gap-2 text-sm leading-5 font-medium",
                  disabled ? "text-secondary" : "text-ink",
                )}
              >
                {option.icon !== undefined && <span className="shrink-0 text-secondary">{option.icon}</span>}
                {option.label}
                {option.tag}
              </span>
              <span className="block text-[13px] leading-5 text-muted">{option.description}</span>
            </span>
          </label>
        );
      })}
    </div>
  );
}
