import { initials } from "../names";
import { cx } from "./cx";
import { toneFor } from "./tone";

interface AvatarProps {
  name: string;
  /** What picks the fill; members use their org's slug so colleagues match. */
  seed?: string;
  size?: number;
  className?: string;
}

/** A member: initials in a filled circle (the sidebar footer, the account switcher). */
export function Avatar({ name, seed, size = 32, className }: AvatarProps) {
  return (
    <span
      aria-hidden
      style={{
        width: size,
        height: size,
        fontSize: Math.max(9, Math.round(size * 0.38)),
        backgroundColor: toneFor(seed ?? name),
      }}
      className={cx(
        "inline-flex shrink-0 items-center justify-center rounded-full leading-none font-medium tracking-[0.02em] text-white select-none",
        className,
      )}
    >
      {initials(name)}
    </span>
  );
}

/** An organization: a monogram in a rounded square (inbox rows, network nodes). */
export function OrgAvatar({ name, seed, size = 28, className }: AvatarProps) {
  return (
    <span
      aria-hidden
      style={{
        width: size,
        height: size,
        fontSize: Math.max(9, Math.round(size * 0.4)),
        backgroundColor: toneFor(seed ?? name),
        borderRadius: Math.round(size * 0.24),
      }}
      className={cx(
        "inline-flex shrink-0 items-center justify-center leading-none font-semibold text-white select-none",
        className,
      )}
    >
      {initials(name, 1)}
    </span>
  );
}
