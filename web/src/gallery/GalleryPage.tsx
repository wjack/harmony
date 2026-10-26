import { Fragment, useEffect, useState, type ReactNode } from "react";
import { createMemoryRouter, useLocation, useNavigate, type RouteObject } from "react-router";
import { RouterProvider } from "react-router/dom";

import { useRealtime } from "../app/context";
import { shellRoutes } from "../fixtures/shell";
import { Spark, cx } from "../shell/ui";
import { loadGallery, slugify } from "./collect";
import { FixtureProviders } from "./FixtureProviders";
import { SectionBoundary } from "./SectionBoundary";
import type { GalleryArea, GallerySection } from "./types";

/**
 * /__gallery (development builds only): every area's components over fixtures, with no
 * backend. Areas register by exporting sections from `web/src/<area>/gallery.tsx` or a
 * file in `web/src/gallery/sections/` (see types.ts).
 */
export function GalleryPage() {
  const [areas, setAreas] = useState<GalleryArea[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    void loadGallery().then((loaded) => {
      if (!cancelled) setAreas(loaded);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Jump to the hash once the sections exist (they load asynchronously).
  useEffect(() => {
    if (areas === null || window.location.hash === "") return;
    document.getElementById(decodeURIComponent(window.location.hash.slice(1)))?.scrollIntoView();
  }, [areas]);

  return (
    <div className="flex h-full bg-canvas">
      <nav
        aria-label="Gallery sections"
        className="scrollbar-quiet flex w-[248px] shrink-0 flex-col overflow-y-auto border-r border-border bg-sidebar px-2 pb-6"
      >
        <div className="flex h-12 items-center gap-2 px-2">
          <Spark size={18} />
          <span className="font-serif text-[19px] tracking-[-0.01em]">Gallery</span>
          <span className="ml-auto rounded-[4px] border border-border-strong px-1.5 font-mono text-[10px] tracking-[0.08em] text-muted">
            DEV
          </span>
        </div>
        {areas?.map((area) => (
          <div key={area.area} className="mt-3">
            <a
              href={`#${area.area}`}
              className="block px-2 pb-1 text-xs leading-4 font-medium tracking-[0.04em] text-muted uppercase hover:text-ink"
            >
              {area.area}
            </a>
            {area.sections.map((section) => (
              <a
                key={sectionId(area, section)}
                href={`#${sectionId(area, section)}`}
                className="block truncate rounded-control px-2 py-1 text-[13px] leading-5 text-secondary hover:bg-hover hover:text-ink"
              >
                {section.title}
              </a>
            ))}
          </div>
        ))}
      </nav>

      <main className="scrollbar-quiet min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[1180px] px-10 pt-10 pb-24">
          <h1 className="font-serif text-[32px] leading-10 tracking-[-0.015em]">Component gallery</h1>
          <p className="mt-1 text-sm text-muted">
            Every surface over fixtures, no backend needed. Sections come from each area&apos;s{" "}
            <code className="font-mono text-[12.5px]">gallery.tsx</code>.
          </p>
          {areas === null ? (
            <p className="mt-10 text-sm text-muted">Loading sections…</p>
          ) : (
            areas.map((area) => <AreaView key={area.area} area={area} />)
          )}
        </div>
      </main>
    </div>
  );
}

function sectionId(area: GalleryArea, section: GallerySection): string {
  return `${area.area}/${section.id ?? slugify(section.title)}`;
}

function AreaView({ area }: { area: GalleryArea }) {
  return (
    <section id={area.area} className="mt-14 scroll-mt-6">
      <h2 className="border-b border-border pb-2 font-serif text-[24px] leading-8 tracking-[-0.01em] capitalize">
        {area.area}
      </h2>
      {area.error !== undefined && (
        <p role="alert" className="mt-4 rounded-control bg-danger-soft px-3 py-2 text-[13px] text-danger">
          {area.error}
        </p>
      )}
      {area.sections.map((section) => (
        <SectionView key={sectionId(area, section)} id={sectionId(area, section)} section={section} />
      ))}
    </section>
  );
}

function SectionView({ id, section }: { id: string; section: GallerySection }) {
  const [routes] = useState(() => ({ ...shellRoutes(), ...section.fixtures }));
  const [router] = useState(() => sectionRouter(section));
  const layout = section.layout ?? "inline";

  return (
    <article id={id} className="mt-9 scroll-mt-6">
      <h3 className="text-[15px] leading-6 font-semibold">{section.title}</h3>
      {section.description !== undefined && <p className="text-[13px] leading-5 text-muted">{section.description}</p>}
      <div
        style={layout === "page" ? { height: section.height ?? 680 } : undefined}
        className={cx(
          "mt-3",
          layout === "page" &&
            // The transform makes this the containing block for fixed-position children.
            "isolate overflow-hidden rounded-menu border border-border shadow-card [transform:translateZ(0)]",
          layout === "inline" && "rounded-menu border border-dashed border-border-strong bg-canvas p-6",
        )}
      >
        <FixtureProviders routes={routes}>
          <RouterProvider router={router} />
        </FixtureProviders>
      </div>
    </article>
  );
}

/**
 * The section's own router: its content at `section.path`, and anything it navigates
 * to elsewhere shown as a note with a way back.
 */
function sectionRouter(section: GallerySection) {
  const path = section.path ?? "*";
  const content = (
    <SectionBoundary>
      <WhenLive>
        <SectionRender render={section.render} />
      </WhenLive>
    </SectionBoundary>
  );
  const routes: RouteObject[] = [{ path, element: content }];
  if (path !== "*") routes.push({ path: "*", element: <LeftSection /> });
  return createMemoryRouter(routes, { initialEntries: [section.url ?? "/"] });
}

function LeftSection() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <p className="p-6 text-[13px] text-muted">
      Navigated to <code className="font-mono">{location.pathname + location.search}</code>, outside this
      section.{" "}
      <button
        type="button"
        className="underline underline-offset-2 hover:text-ink"
        onClick={() => {
          void navigate(-1);
        }}
      >
        Back
      </button>
    </p>
  );
}

/** Calls `render` inside the section's providers, so hooks in it see the fixtures. */
function SectionRender({ render }: { render: () => ReactNode }) {
  return <>{render()}</>;
}

/** Render once the fixture session is cached, and remount on an account switch, as the app does. */
function WhenLive({ children }: { children: ReactNode }) {
  const generation = useRealtime((state) => state.generation);
  if (generation === 0) return null;
  return <Fragment key={generation}>{children}</Fragment>;
}
