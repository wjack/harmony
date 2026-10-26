import { createElement, isValidElement } from "react";
import { describe, expect, it } from "vitest";

import { areaOf, humanize, loadGallery, sectionsOf } from "./collect";

function OutreachCardGallery() {
  return null;
}

describe("sectionsOf", () => {
  it("reads a default-exported section list", () => {
    const sections = sectionsOf(
      { default: [{ title: "Inbox", layout: "page", height: 600, fixtures: {}, render: () => "inbox" }] },
      "requests",
    );
    expect(sections).toHaveLength(1);
    expect(sections[0]).toMatchObject({ title: "Inbox", layout: "page", height: 600, fixtures: {} });
    expect(sections[0]?.render()).toBe("inbox");
  });

  it("accepts components, elements and named lists", () => {
    const element = createElement("p", null, "hi");
    const sections = sectionsOf(
      {
        sections: [
          { name: "As a component", component: OutreachCardGallery },
          { label: "As an element", element },
          OutreachCardGallery,
          { title: "Ignored: nothing to render" },
        ],
      },
      "chat",
    );
    expect(sections.map((section) => section.title)).toEqual([
      "As a component",
      "As an element",
      "Outreach card gallery",
    ]);
    expect(isValidElement(sections[0]?.render())).toBe(true);
    expect(sections[1]?.render()).toBe(element);
  });

  it("falls back to every exported component", () => {
    const sections = sectionsOf({ OutreachCardGallery, helper: () => null, CONSTANT: 3 }, "chat");
    expect(sections.map((section) => section.title)).toEqual(["Outreach card gallery"]);
  });

  it("finds nothing in a module without sections", () => {
    expect(sectionsOf({ helper: () => null }, "network")).toEqual([]);
    expect(sectionsOf(null, "network")).toEqual([]);
  });
});

describe("loadGallery", () => {
  it("puts the shell first and reports broken or empty modules instead of failing", async () => {
    const areas = await loadGallery({
      "../requests/gallery.tsx": () => Promise.resolve({ default: [{ title: "Inbox", render: () => null }] }),
      "../shell/gallery.tsx": () => Promise.resolve({ default: [{ title: "App shell", render: () => null }] }),
      "../network/gallery.tsx": () => Promise.reject(new Error("Failed to fetch dynamically imported module")),
      "./sections/empty.tsx": () => Promise.resolve({}),
    });
    expect(areas.map((area) => [area.area, area.error ?? null])).toEqual([
      ["shell", null],
      ["empty", "No sections exported."],
      ["network", "Failed to fetch dynamically imported module"],
      ["requests", null],
    ]);
  });

  it("names areas after their directory or section file", () => {
    expect(areaOf("../customize/gallery.tsx")).toBe("customize");
    expect(areaOf("./sections/outreach-cards.tsx")).toBe("outreach-cards");
    expect(humanize("network_view")).toBe("Network view");
  });
});
