import type { ReactNode } from "react";

import type { FixtureRoutes } from "../fixtures";

/**
 * One gallery entry. An area's `gallery.tsx` (web/src/<area>/gallery.tsx) or a file in
 * web/src/gallery/sections/ default-exports an array of these:
 *
 *   export default [
 *     { title: "Outreach card", fixtures: chatRoutes, render: () => <OutreachCard … /> },
 *   ] satisfies GallerySection[];
 *
 * The gallery also accepts `export const sections = [...]`, or plain exported
 * components (one entry each, titled from the component's name).
 */
export interface GallerySection {
  /** Stable id for the URL hash; defaults to the title, slugified. */
  id?: string;
  title: string;
  description?: string;
  /** Fixture routes the section's components read, merged over `shellRoutes()`. */
  fixtures?: FixtureRoutes;
  /**
   * "inline" (default) pads the component on the canvas; "page" gives it a full-size
   * app frame (fixed-position children stay inside it); "bare" renders it as-is.
   */
  layout?: "inline" | "page" | "bare";
  /** Frame height for `layout: "page"`, in px (default 680). */
  height?: number;
  /**
   * The section runs in its own memory router. `path` is its route pattern (default
   * "*"), e.g. "/requests/:threadId", and `url` the location it starts at (default "/").
   */
  path?: string;
  url?: string;
  render: () => ReactNode;
}

export interface GalleryArea {
  /** The directory the sections came from ("chat", "requests"…). */
  area: string;
  sections: GallerySection[];
  /** Set when the module failed to load or had no sections. */
  error?: string;
}
