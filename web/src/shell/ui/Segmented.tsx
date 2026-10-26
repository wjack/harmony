import type { ReactNode } from "react";

import { cx } from "./cx";

export interface SegmentedOption<T extends string> {
  value: T;
  label: ReactNode;
  disabled?: boolean;
}

interface SegmentedProps<T extends string> {
  value: T;
  options: readonly SegmentedOption<T>[];
  onChange: (value: T) => void;
  /** Names the group for assistive tech. */
  label: string;
  className?: string;
}

/** Segmented pill choices, black when selected (UI_REFERENCE.md §9, the modal pattern). */
export function Segmented<T extends string>({ value, options, onChange, label, className }: SegmentedProps<T>) {
  return (
    <div role="radiogroup" aria-label={label} className={cx("inline-flex flex-wrap gap-1.5", className)}>
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={option.disabled}
            onClick={() => {
              onChange(option.value);
            }}
            className={cx(
              "h-8 rounded-full px-3.5 text-[13px] font-medium transition-colors duration-100",
              selected
                ? "bg-primary text-on-primary"
                : "border border-border-strong bg-surface text-secondary hover:bg-hover hover:text-ink disabled:text-faint",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
