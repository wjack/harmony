import { ChevronDown } from "lucide-react";
import type { ReactNode } from "react";

import { cx, Menu, MenuCheckItem, MenuContent, MenuTrigger } from "../shell/ui";
import { DISABLED_CONTROL } from "./fields";

export interface SelectOption<T extends string> {
  value: T;
  label: string;
  /** A muted second line in the menu. */
  description?: string;
  /** At most 20px wide. */
  icon?: ReactNode;
}

interface SelectProps<T extends string> {
  id?: string;
  /** Names the control when no `<label>` points at it. */
  "aria-label"?: string;
  value: T;
  options: readonly SelectOption<T>[];
  onChange: (value: T) => void;
  disabled?: boolean;
  /** Shown when `value` matches no option. */
  placeholder?: string;
  className?: string;
  /** Menu width in px (defaults to the trigger's). */
  menuWidth?: number;
}

/** Claude's select: an input-styled button with a chevron, opening a check-marked menu. */
export function Select<T extends string>({
  id,
  "aria-label": ariaLabel,
  value,
  options,
  onChange,
  disabled = false,
  placeholder = "Choose…",
  className,
  menuWidth,
}: SelectProps<T>) {
  const current = options.find((option) => option.value === value);
  // When some options have an icon, the rest keep its slot empty so the labels line up.
  const iconSlot = options.some((option) => option.icon !== undefined);
  return (
    <Menu>
      <MenuTrigger asChild disabled={disabled}>
        <button
          id={id}
          type="button"
          // The name keeps the current value, as a labelled select's would.
          aria-label={ariaLabel === undefined ? undefined : `${ariaLabel}: ${current?.label ?? placeholder}`}
          className={cx(
            "flex h-9 w-full items-center gap-2 rounded-control border border-border-strong bg-surface px-3 text-left text-sm text-ink transition-colors outline-none enabled:hover:bg-canvas focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/15 focus-visible:outline-none data-[state=open]:border-accent",
            DISABLED_CONTROL,
            className,
          )}
        >
          {current?.icon !== undefined && <span className="shrink-0 text-secondary">{current.icon}</span>}
          <span className={cx("min-w-0 flex-1 truncate", current === undefined && "text-faint")}>
            {current?.label ?? placeholder}
          </span>
          {!disabled && <ChevronDown size={16} strokeWidth={1.75} className="shrink-0 text-muted" aria-hidden />}
        </button>
      </MenuTrigger>
      <MenuContent
        align="start"
        style={{ width: menuWidth ?? "var(--radix-dropdown-menu-trigger-width)" }}
        // A short list shows in full; only the viewport's edge makes it scroll.
        className="max-h-[var(--radix-dropdown-menu-content-available-height)] overflow-y-auto"
      >
        {options.map((option) => (
          <MenuCheckItem
            key={option.value}
            checked={option.value === value}
            icon={iconSlot ? <span className="flex w-5 justify-center">{option.icon}</span> : undefined}
            onSelect={() => {
              onChange(option.value);
            }}
          >
            <span className="block truncate">{option.label}</span>
            {option.description !== undefined && (
              <span className="block text-[13px] leading-5 text-muted">{option.description}</span>
            )}
          </MenuCheckItem>
        ))}
      </MenuContent>
    </Menu>
  );
}
