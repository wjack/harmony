import { describe, expect, it } from "vitest";

import type { MemberView } from "../api/types";
import { memberDirectory, members, story } from "../fixtures/shell";
import { menuAccounts } from "./cast";

const ref = ({ id, slug, name }: MemberView) => ({ id, slug, name });
const step = (id: string, actAs: MemberView | null) => ({ id, title: id, body: "", act_as: actAs && ref(actAs), done_on: null });
const slugs = (list: readonly MemberView[]) => list.map((member) => member.slug);

describe("menuAccounts", () => {
  const walkthrough = {
    ...story,
    steps: [step("ask", members.alex), step("routed", members.helen), step("again", members.alex), step("big-run", null)],
  };

  it("puts the walkthrough's people and the acting member at the top, everyone else in the submenu", () => {
    const { shown, more } = menuAccounts(memberDirectory, walkthrough, members.raj.id);
    expect(slugs(shown)).toEqual(["alex", "helen", "raj"]);
    expect(slugs(more)).toEqual(["priya", "maya", "dana", "jordan", "lena", "sam"]);
  });

  it("shows the featured cast when there's no walkthrough", () => {
    const { shown, more } = menuAccounts(memberDirectory, story, members.alex.id);
    expect(shown).toEqual(memberDirectory.featured);
    expect(more).toEqual(memberDirectory.all);
  });
});
