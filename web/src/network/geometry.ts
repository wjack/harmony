/** Canvas geometry: how the view is framed. */

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Viewport {
  x: number;
  y: number;
  zoom: number;
}

// More air on the left, so the zoom controls (bottom left) never sit on a card.
const PADDING = { top: 32, right: 24, bottom: 32, left: 56 } as const;
const MAX_ZOOM = 1.1;

/**
 * The least zoom the canvas rests at: a card's name stays legible (13px → 9.4px), and
 * the story's graph still fits whole beside the side panel from about 1100px wide (on
 * narrower windows the app sidebar shows its rail while the panel is open, narrow.ts).
 * A graph too big for it (the big run) overflows instead, and the visitor pans or uses
 * "Fit view".
 */
export const READABLE_ZOOM = 0.72;

/** "Fit view": the whole graph, however small that makes it, never past 110%. */
export const FIT_OPTIONS = {
  padding: {
    top: `${PADDING.top}px`,
    right: `${PADDING.right}px`,
    bottom: `${PADDING.bottom}px`,
    left: `${PADDING.left}px`,
  },
  maxZoom: MAX_ZOOM,
} as const;

/** The smallest rect around all of `rects` (a point at the origin when there are none). */
export function boundsOf(rects: readonly Rect[]): Rect {
  if (rects.length === 0) return { x: 0, y: 0, width: 0, height: 0 };
  const left = Math.min(...rects.map((rect) => rect.x));
  const top = Math.min(...rects.map((rect) => rect.y));
  const right = Math.max(...rects.map((rect) => rect.x + rect.width));
  const bottom = Math.max(...rects.map((rect) => rect.y + rect.height));
  return { x: left, y: top, width: right - left, height: bottom - top };
}

/**
 * The canvas's resting frame for a `width`×`height` pane: the whole graph, centred,
 * when it fits at a readable zoom; otherwise the readable zoom, with each axis that
 * overflows pinned at its start (the requester's side and the top), so the canvas
 * opens on the request rather than on a thumbnail of the network.
 */
export function readableFrame(bounds: Rect, width: number, height: number): Viewport {
  const innerWidth = Math.max(1, width - PADDING.left - PADDING.right);
  const innerHeight = Math.max(1, height - PADDING.top - PADDING.bottom);
  const fit = Math.min(innerWidth / Math.max(1, bounds.width), innerHeight / Math.max(1, bounds.height));
  const zoom = Math.min(MAX_ZOOM, Math.max(READABLE_ZOOM, fit));
  const place = (start: number, size: number, inner: number, padding: number) => {
    const scaled = size * zoom;
    const offset = scaled <= inner ? (inner - scaled) / 2 : 0;
    return padding + offset - start * zoom;
  };
  return {
    x: place(bounds.x, bounds.width, innerWidth, PADDING.left),
    y: place(bounds.y, bounds.height, innerHeight, PADDING.top),
    zoom,
  };
}
