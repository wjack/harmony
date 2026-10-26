/**
 * The CSS variables in tokens.css, for code that styles through props rather than
 * classes (SVG strokes, React Flow edges, inline styles). Each value is a `var(...)`
 * reference, so it follows the active token set.
 */
export const TOKEN_NAMES = [
  "bg",
  "bg-sidebar",
  "bg-user-bubble",
  "surface",
  "hover",
  "pressed",
  "scrim",
  "border",
  "border-strong",
  "text",
  "text-secondary",
  "text-muted",
  "text-faint",
  "accent",
  "accent-strong",
  "accent-soft",
  "primary-btn",
  "primary-btn-text",
  "toggle-on",
  "link",
  "tooltip-bg",
  "tooltip-text",
  "info",
  "info-soft",
  "success",
  "success-soft",
  "warning",
  "warning-soft",
  "danger",
  "danger-soft",
] as const;

export type TokenName = (typeof TOKEN_NAMES)[number];

export function token(name: TokenName): string {
  return `var(--${name})`;
}

/** The resolved value of a token on this page, for canvas or other non-CSS consumers. */
export function resolveToken(name: TokenName, element: Element = document.documentElement): string {
  return getComputedStyle(element).getPropertyValue(`--${name}`).trim();
}

export const FONT = {
  serif: "var(--font-serif)",
  sans: "var(--font-sans)",
  mono: "var(--font-mono)",
} as const;
