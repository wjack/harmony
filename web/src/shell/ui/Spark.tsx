import { cx } from "./cx";

// Twelve irregular rays, as in Claude's mark; the irregularity is what makes it read
// as the spark rather than an asterisk. Each ray runs from just off-center outwards.
const RAYS: readonly (readonly [number, number, number, number])[] = [
  [12, 10.5, 12, 1.6],
  [12.73, 10.69, 16.41, 4.04],
  [13.27, 11.21, 21.07, 6.33],
  [13.5, 11.95, 21.49, 11.67],
  [13.31, 12.73, 21.01, 16.99],
  [12.77, 13.29, 16.64, 19.71],
  [12.05, 13.5, 12.37, 22.59],
  [11.3, 13.32, 7.63, 20.21],
  [10.71, 12.77, 3, 17.41],
  [10.5, 12, 2.3, 12],
  [10.69, 11.27, 2.55, 6.76],
  [11.25, 10.7, 7.4, 4.03],
];

interface SparkProps {
  size?: number;
  /** The streaming indicator: the spark turning on its own line. */
  animated?: boolean;
  className?: string;
  /** Accessible name; decorative (hidden) when omitted. */
  label?: string;
}

/** The Claude spark, in `--accent`. */
export function Spark({ size = 20, animated = false, className, label }: SparkProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      className={cx("shrink-0 text-accent", animated && "animate-spark", className)}
      role={label === undefined ? undefined : "img"}
      aria-label={label}
      aria-hidden={label === undefined ? true : undefined}
    >
      <g stroke="currentColor" strokeWidth={2.2} strokeLinecap="round">
        {RAYS.map(([x1, y1, x2, y2]) => (
          <line key={`${x2}-${y2}`} x1={x1} y1={y1} x2={x2} y2={y2} />
        ))}
      </g>
    </svg>
  );
}
