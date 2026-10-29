/**
 * Where the "What's on screen" overlay's callouts go (HelpOverlay.tsx). Each starts on
 * its own side of its control, under a demo-bar button or right of a sidebar row, and
 * moves only as far as it must to stay clear of the other callouts, every control,
 * every arrow and the viewport's edges, sooner aside than further out, so the bar's
 * callouts keep to one row while they fit. One that finds no room is left out: an
 * extra one first, since they are placed last. Every ring sits the same distance out
 * from its control, closer in where two controls are too close for two full rings.
 */

/** Viewport pixels, as `getBoundingClientRect()` gives them. */
export interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface Size {
  width: number;
  height: number;
}

/** Under a control in the demo bar, or right of a row in the sidebar. */
export type Side = "below" | "right";

export interface CalloutSpec extends Size {
  key: string;
  side: Side;
  /** As much of the control as is on screen. */
  target: Box;
  /** Placed after the others, so it is the one left out when room runs short. */
  extra?: boolean;
}

export interface Placement {
  key: string;
  target: Box;
  /** The ring drawn around the control. */
  ring: Box;
  /** The callout, at its measured size. */
  box: Box;
  /** The arrow's SVG path, from the callout to its control. */
  arrow: string;
}

interface Point {
  x: number;
  y: number;
}

/** A cubic Bézier curve. */
interface Curve {
  from: Point;
  c1: Point;
  c2: Point;
  to: Point;
}

/** The ring's distance outside its control, where its neighbors leave room. */
export const RING = 4;
/** Kept between two controls' rings, so each reads as its own. */
const APART = 4;
/** Between a control and its callout, where the arrow runs. */
const GAP = 36;
/** Kept clear around callouts and controls. */
const SPACE = 12;
/** Kept clear of the viewport's edges. */
const MARGIN = 12;
/** How far a callout may move from its first spot, in steps: past that, a long arrow is worse than none. */
const REACH = 240;
const STEP = 12;
/** An arrow leaves its callout at least this far from a corner. */
const INSET = 20;
/** Its head stops this far outside the ring. */
const TIP = 3;
/** Where along an arrow it is checked for running into things. */
const SAMPLES = Array.from({ length: 15 }, (_, i) => (i + 1) / 16);

/** A callout's moves from its first spot, out from its control and aside, cheapest first. */
const MOVES = Array.from({ length: REACH / STEP + 1 }, (_, out) =>
  Array.from({ length: (2 * REACH) / STEP + 1 }, (_, aside) => ({ out: out * STEP, aside: aside * STEP - REACH })),
)
  .flat()
  .sort((a, b) => cost(a) - cost(b));

/** Moving out costs twice what moving aside does. */
function cost({ out, aside }: { out: number; aside: number }): number {
  return 2 * out + Math.abs(aside);
}

export function placeCallouts(
  specs: readonly CalloutSpec[],
  viewport: Size,
  /** Anything else callouts and their arrows keep off: the "?", the network card and the close button. */
  obstacles: readonly Box[] = [],
): Placement[] {
  const placed: { box: Box; arrow: Point[] }[] = [];
  const placements: Placement[] = [];
  // From the viewport's edges in: a callout by an edge has the least room, and the ones
  // further in then step away from it instead of crossing its arrow.
  const order = [...specs].sort(
    (a, b) => Number(a.extra === true) - Number(b.extra === true) || edgeRoom(a, viewport) - edgeRoom(b, viewport),
  );
  const keepOff = [...specs.map((spec) => spec.target), ...obstacles].map((box) => grow(box, SPACE));
  const blocked = obstacles.map((box) => grow(box, RING));
  const ring = ringDistance(specs.map((spec) => spec.target));
  for (const spec of order) {
    const others = specs.filter((other) => other !== spec).map((other) => grow(other.target, ring));
    for (const box of candidates(spec, viewport)) {
      if (keepOff.some((other) => overlaps(box, other))) continue;
      if (placed.some((other) => overlaps(box, grow(other.box, SPACE)))) continue;
      // Neither its arrow nor an earlier one may cross a callout or another control.
      const curve = arrowCurve(spec, box, ring + TIP);
      const arrow = SAMPLES.map((t) => pointOn(curve, t));
      const crossed = [...others, ...blocked, ...placed.map((other) => grow(other.box, RING))];
      if (arrow.some((point) => crossed.some((other) => contains(other, point)))) continue;
      if (placed.some((other) => other.arrow.some((point) => contains(grow(box, RING), point)))) continue;
      placed.push({ box, arrow });
      placements.push({ key: spec.key, target: spec.target, ring: grow(spec.target, ring), box, arrow: pathOf(curve) });
      break;
    }
  }
  return placements;
}

/**
 * How far outside its control every ring sits: `RING`, or less, down to none, where two
 * controls are closer than two rings and `APART` between them.
 */
export function ringDistance(targets: readonly Box[]): number {
  let distance = RING;
  for (const [i, a] of targets.entries()) {
    for (const b of targets.slice(i + 1)) distance = Math.min(distance, (separation(a, b) - APART) / 2);
  }
  return Math.max(0, distance);
}

/** The gap between two boxes, across or down, whichever is wider; below zero when they overlap. */
function separation(a: Box, b: Box): number {
  return Math.max(b.left - a.right, a.left - b.right, b.top - a.bottom, a.top - b.bottom);
}

export function grow(box: Box, by: number): Box {
  return { left: box.left - by, top: box.top - by, right: box.right + by, bottom: box.bottom + by };
}

/** How near the viewport's edge the control sits, along the way its callout moves aside. */
function edgeRoom({ side, target }: CalloutSpec, viewport: Size): number {
  if (side === "below") {
    const x = (target.left + target.right) / 2;
    return Math.min(x, viewport.width - x);
  }
  const y = (target.top + target.bottom) / 2;
  return Math.min(y, viewport.height - y);
}

/**
 * The callout's spots, nearest first: its first spot (centered under its control, or
 * level with it on its right), then further out and further aside, inside the viewport.
 */
function* candidates({ side, target, width, height }: CalloutSpec, viewport: Size): Generator<Box> {
  const first =
    side === "below"
      ? { x: (target.left + target.right - width) / 2, y: target.bottom + GAP }
      : { x: target.right + GAP, y: (target.top + target.bottom - height) / 2 };
  for (const { out, aside } of MOVES) {
    const x = clamp(first.x + (side === "below" ? aside : out), MARGIN, viewport.width - MARGIN - width);
    const y = clamp(first.y + (side === "below" ? out : aside), MARGIN, viewport.height - MARGIN - height);
    yield { left: x, top: y, right: x + width, bottom: y + height };
  }
}

/**
 * From the callout's edge facing the control to just outside its ring: straight when
 * they line up, else an S that leaves and arrives square to each.
 */
function arrowCurve({ side, target }: CalloutSpec, box: Box, tip: number): Curve {
  if (side === "below") {
    const to = { x: (target.left + target.right) / 2, y: target.bottom + tip };
    const from = { x: clamp(to.x, box.left + INSET, box.right - INSET), y: box.top };
    const bend = (from.y - to.y) / 2;
    return { from, c1: { x: from.x, y: from.y - bend }, c2: { x: to.x, y: to.y + bend }, to };
  }
  const to = { x: target.right + tip, y: (target.top + target.bottom) / 2 };
  const from = { x: box.left, y: clamp(to.y, box.top + INSET, box.bottom - INSET) };
  const bend = (from.x - to.x) / 2;
  return { from, c1: { x: from.x - bend, y: from.y }, c2: { x: to.x + bend, y: to.y }, to };
}

function pointOn({ from, c1, c2, to }: Curve, t: number): Point {
  const u = 1 - t;
  const [a, b, c, d] = [u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t];
  return { x: a * from.x + b * c1.x + c * c2.x + d * to.x, y: a * from.y + b * c1.y + c * c2.y + d * to.y };
}

function pathOf({ from, c1, c2, to }: Curve): string {
  const at = ({ x, y }: Point) => `${x.toFixed(1)} ${y.toFixed(1)}`;
  return `M ${at(from)} C ${at(c1)}, ${at(c2)}, ${at(to)}`;
}

function overlaps(a: Box, b: Box): boolean {
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

function contains(box: Box, { x, y }: Point): boolean {
  return x > box.left && x < box.right && y > box.top && y < box.bottom;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(value, max));
}
