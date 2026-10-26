import type { ReactNode } from "react";

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

/** Radio rows in a bordered list box, the way Claude's modals list choices. */
export function RadioList<T extends string>({ name, label, value, options, onChange, disabled }: RadioListProps<T>) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className="divide-y divide-border overflow-hidden rounded-menu border border-border bg-surface"
    >
      {options.map((option) => {
        const checked = option.value === value;
        return (
          <label
            key={option.value}
            className="flex cursor-pointer items-start gap-3 px-4 py-3 transition-colors has-[:disabled]:cursor-default hover:bg-canvas has-[:disabled]:hover:bg-surface"
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
              className={
                checked
                  ? "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full bg-primary peer-focus-visible:ring-2 peer-focus-visible:ring-accent/40"
                  : "mt-0.5 size-4 shrink-0 rounded-full border border-border-strong bg-surface peer-focus-visible:ring-2 peer-focus-visible:ring-accent/40"
              }
            >
              {checked && <span className="size-1.5 rounded-full bg-surface" />}
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2 text-sm leading-5 font-medium text-ink">
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
