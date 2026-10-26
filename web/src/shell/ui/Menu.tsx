import { Check, ChevronRight } from "lucide-react";
import { DropdownMenu } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";

import { cx } from "./cx";

/**
 * Claude's popover menus (account menu, `+` menu, row `…` menus): white, 12px radius,
 * a hairline and a soft shadow; rows with a leading icon, a hover tint and an optional
 * shortcut or chevron on the right. Built on Radix's dropdown menu for keyboard
 * navigation, typeahead and submenus.
 */
export const Menu = DropdownMenu.Root;
export const MenuTrigger = DropdownMenu.Trigger;
export const MenuGroup = DropdownMenu.Group;
export const MenuSub = DropdownMenu.Sub;

const PANEL =
  "z-50 min-w-[220px] rounded-menu border border-border bg-surface p-1.5 text-ink shadow-menu outline-none data-[state=open]:animate-pop-in";

export function MenuContent({ className, children, ...props }: ComponentProps<typeof DropdownMenu.Content>) {
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.Content sideOffset={6} collisionPadding={8} className={cx(PANEL, className)} {...props}>
        {children}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  );
}

const ROW =
  "relative flex min-h-8 cursor-default items-center gap-2.5 rounded-control px-2 py-1.5 text-sm leading-5 outline-none select-none data-[disabled]:text-faint data-[highlighted]:bg-hover data-[state=open]:bg-hover";

interface RowParts {
  icon?: ReactNode;
  /** Right-aligned hint, e.g. "⇧⌘,". */
  shortcut?: ReactNode;
}

export function MenuItem({
  icon,
  shortcut,
  className,
  children,
  ...props
}: ComponentProps<typeof DropdownMenu.Item> & RowParts) {
  return (
    <DropdownMenu.Item className={cx(ROW, className)} {...props}>
      {icon !== undefined && <span className="flex size-[18px] shrink-0 items-center justify-center text-secondary">{icon}</span>}
      <span className="min-w-0 flex-1">{children}</span>
      {shortcut !== undefined && <span className="ml-4 text-xs tracking-wide text-muted">{shortcut}</span>}
    </DropdownMenu.Item>
  );
}

/** A row with a ✓ on the right when `checked` (the active account, the chosen language). */
export function MenuCheckItem({
  checked,
  icon,
  className,
  children,
  ...props
}: ComponentProps<typeof DropdownMenu.Item> & { checked: boolean; icon?: ReactNode }) {
  return (
    <DropdownMenu.Item className={cx(ROW, className)} aria-checked={checked} role="menuitemradio" {...props}>
      {icon !== undefined && <span className="flex shrink-0 items-center justify-center">{icon}</span>}
      <span className="min-w-0 flex-1">{children}</span>
      {checked && <Check size={16} strokeWidth={1.75} className="ml-3 shrink-0 text-ink" aria-hidden />}
    </DropdownMenu.Item>
  );
}

export function MenuSubTrigger({
  icon,
  className,
  children,
  ...props
}: ComponentProps<typeof DropdownMenu.SubTrigger> & { icon?: ReactNode }) {
  return (
    <DropdownMenu.SubTrigger className={cx(ROW, className)} {...props}>
      {icon !== undefined && <span className="flex size-[18px] shrink-0 items-center justify-center text-secondary">{icon}</span>}
      <span className="min-w-0 flex-1">{children}</span>
      <ChevronRight size={16} strokeWidth={1.5} className="ml-3 shrink-0 text-muted" aria-hidden />
    </DropdownMenu.SubTrigger>
  );
}

export function MenuSubContent({ className, children, ...props }: ComponentProps<typeof DropdownMenu.SubContent>) {
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.SubContent sideOffset={8} alignOffset={-6} collisionPadding={8} className={cx(PANEL, className)} {...props}>
        {children}
      </DropdownMenu.SubContent>
    </DropdownMenu.Portal>
  );
}

export function MenuSeparator({ className }: { className?: string }) {
  return <DropdownMenu.Separator className={cx("mx-2 my-1.5 h-px bg-border", className)} />;
}

/** Muted, non-interactive text: the email line, or a group's label. */
export function MenuLabel({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <DropdownMenu.Label className={cx("truncate px-2 pt-1.5 pb-1 text-[13px] leading-5 text-muted", className)}>
      {children}
    </DropdownMenu.Label>
  );
}
