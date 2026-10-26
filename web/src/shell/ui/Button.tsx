import type { ButtonHTMLAttributes, ReactNode, Ref } from "react";

import { cx } from "./cx";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

const VARIANTS: Record<ButtonVariant, string> = {
  // "Save changes", "New chat", "Export": black, 8px radius.
  primary: "bg-primary text-on-primary hover:bg-primary-hover disabled:opacity-40",
  // "Cancel": white with a hairline.
  secondary:
    "border border-border-strong bg-surface text-ink shadow-[0_1px_1px_rgb(20_20_19/0.03)] hover:bg-hover disabled:text-faint",
  ghost: "text-secondary hover:bg-hover hover:text-ink disabled:text-faint",
  danger: "bg-danger text-white hover:brightness-95 disabled:opacity-40",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "h-7 gap-1.5 px-2.5 text-[13px]",
  md: "h-8 gap-1.5 px-3 text-sm",
  lg: "h-9 gap-2 px-4 text-sm",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Leading icon. */
  icon?: ReactNode;
  ref?: Ref<HTMLButtonElement>;
}

export function Button({
  variant = "secondary",
  size = "md",
  icon,
  className,
  children,
  type = "button",
  ref,
  ...rest
}: ButtonProps) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx(
        "inline-flex shrink-0 items-center justify-center rounded-control font-medium whitespace-nowrap transition-colors duration-100 select-none",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {icon}
      {children}
    </button>
  );
}

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** The accessible name (also the tooltip when wrapped in `<Tooltip>`). */
  label: string;
  size?: "sm" | "md";
  ref?: Ref<HTMLButtonElement>;
}

/** A square, borderless icon button with a hover tint (sidebar toggle, ×, ⋯). */
export function IconButton({
  label,
  size = "md",
  className,
  children,
  type = "button",
  ref,
  ...rest
}: IconButtonProps) {
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      className={cx(
        "inline-flex shrink-0 items-center justify-center rounded-control text-muted transition-colors duration-100 hover:bg-hover hover:text-ink disabled:opacity-40",
        size === "sm" ? "size-7" : "size-8",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
