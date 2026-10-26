import { createElement, type ComponentType, type ReactNode } from "react";

import type { GalleryArea, GallerySection } from "./types";

type Loader = () => Promise<unknown>;

// Each area's gallery.tsx, plus loose section files in this directory. Lazy, so a
// broken section file shows as an error card instead of taking the gallery down.
const MODULES: Record<string, Loader> = import.meta.glob(["../*/gallery.tsx", "./sections/*.tsx"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isComponentName(name: string): boolean {
  return /^[A-Z]/.test(name);
}

/** "OutreachCardGallery" → "Outreach card gallery". */
export function humanize(name: string): string {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[-_]+/g, " ")
    .trim()
    .toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

export function areaOf(path: string): string {
  const parts = path.split("/");
  const file = parts.at(-1) ?? path;
  if (file === "gallery.tsx") return parts.at(-2) ?? path;
  return file.replace(/\.tsx$/, "");
}

function fromComponent(title: string, component: ComponentType): GallerySection {
  return { title, render: () => createElement(component) };
}

function fromRecord(value: Record<string, unknown>): GallerySection | null {
  const title = [value.title, value.name, value.label].find((field) => typeof field === "string");
  if (typeof title !== "string") return null;
  const base: Omit<GallerySection, "render"> = {
    title,
    ...(typeof value.id === "string" ? { id: value.id } : {}),
    ...(typeof value.description === "string" ? { description: value.description } : {}),
    ...(value.layout === "page" || value.layout === "inline" || value.layout === "bare"
      ? { layout: value.layout }
      : {}),
    ...(typeof value.height === "number" ? { height: value.height } : {}),
    ...(typeof value.path === "string" ? { path: value.path } : {}),
    ...(typeof value.url === "string" ? { url: value.url } : {}),
    ...(isRecord(value.fixtures)
      ? { fixtures: value.fixtures }
      : isRecord(value.routes)
        ? { fixtures: value.routes }
        : {}),
  };
  const { render, component, Component, element } = value;
  if (typeof render === "function") return { ...base, render: render as () => ReactNode };
  const asComponent = component ?? Component;
  if (typeof asComponent === "function") {
    return { ...base, render: () => createElement(asComponent as ComponentType) };
  }
  if (element !== undefined) return { ...base, render: () => element as ReactNode };
  return null;
}

function toSections(value: unknown, fallbackTitle: string): GallerySection[] {
  if (Array.isArray(value)) return value.flatMap((entry) => toSections(entry, fallbackTitle));
  if (typeof value === "function") {
    const name = value.name;
    return [fromComponent(humanize(name !== "" ? name : fallbackTitle), value as ComponentType)];
  }
  if (isRecord(value)) {
    if (Array.isArray(value.sections)) return toSections(value.sections, fallbackTitle);
    const section = fromRecord(value);
    return section === null ? [] : [section];
  }
  return [];
}

/** Read whatever shape a gallery module exports into sections (see types.ts). */
export function sectionsOf(module: unknown, area: string): GallerySection[] {
  if (!isRecord(module)) return [];
  for (const key of ["default", "sections", "gallery", "gallerySections"]) {
    const sections = toSections(module[key], area);
    if (sections.length > 0) return sections;
  }
  return Object.entries(module)
    .filter(([name, value]) => isComponentName(name) && typeof value === "function")
    .map(([name, value]) => fromComponent(humanize(name), value as ComponentType));
}

// The shell's own areas first, then everyone else's alphabetically.
const AREA_ORDER = ["shell", "demo"];

function areaRank(area: string): number {
  const index = AREA_ORDER.indexOf(area);
  return index === -1 ? AREA_ORDER.length : index;
}

export async function loadGallery(modules: Record<string, Loader> = MODULES): Promise<GalleryArea[]> {
  const areas = await Promise.all(
    Object.entries(modules).map(async ([path, load]): Promise<GalleryArea> => {
      const area = areaOf(path);
      try {
        const sections = sectionsOf(await load(), area);
        return sections.length > 0 ? { area, sections } : { area, sections, error: "No sections exported." };
      } catch (error) {
        return { area, sections: [], error: error instanceof Error ? error.message : String(error) };
      }
    }),
  );
  return areas.sort((a, b) => areaRank(a.area) - areaRank(b.area) || a.area.localeCompare(b.area));
}
