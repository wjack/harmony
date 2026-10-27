import { describe, expect, it } from "vitest";

import { mentionAt, mentionedSlugs, mentionParts, mentionsMember } from "./mentions";

const team = [{ slug: "raj" }, { slug: "helen" }, { slug: "mei-lin" }];

describe("mentionedSlugs", () => {
  it("reads mentions as the server does: distinct lowercase slugs in order, not email addresses", () => {
    // The server's own case (test_e4_domain.py).
    expect(mentionedSlugs("@raj can you check? cc @helen, @raj. me@example.com")).toEqual(["raj", "helen"]);
    expect(mentionedSlugs("Loop in Raj (@raj) on the price")).toEqual(["raj"]);
    expect(mentionedSlugs("@mei-lin, and @mei-lin- too")).toEqual(["mei-lin"]);
    expect(mentionedSlugs("@Raj can you check?")).toEqual([]);
    expect(mentionedSlugs("café@raj, x@@raj, under_@raj")).toEqual([]);
  });
});

describe("mentionsMember", () => {
  it("counts only a slug that names a member", () => {
    expect(mentionsMember("Quote it @85 per sample instead", team)).toBe(false);
    expect(mentionsMember("@Raj can you check?", team)).toBe(false);
    expect(mentionsMember("Loop in Raj (@raj) on the price", team)).toBe(true);
  });
});

describe("mentionParts", () => {
  it("cuts out only the mentions given, leaving other @ text plain", () => {
    expect(mentionParts("@raj and @nobody, see me@example.com", new Set(["raj"]))).toEqual([
      { text: "@raj", mention: true },
      { text: " and @nobody, see me@example.com", mention: false },
    ]);
    expect(mentionParts("@Raj can you check?", new Set(["raj"]))).toEqual([{ text: "@Raj can you check?", mention: false }]);
    expect(mentionParts("", new Set(["raj"]))).toEqual([]);
  });
});

describe("mentionAt", () => {
  it("finds the partial at the caret wherever the server would take it as a mention", () => {
    expect(mentionAt("Loop in (@Ra", 12)).toEqual({ start: 9, query: "ra" });
    expect(mentionAt("@", 1)).toEqual({ start: 0, query: "" });
    expect(mentionAt("me@ex", 5)).toBeNull();
    expect(mentionAt("@raj please", 11)).toBeNull();
  });
});
