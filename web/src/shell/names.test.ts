import { describe, expect, it } from "vitest";

import { members } from "../fixtures/shell";
import { firstName, initials, memberSubtitle } from "./names";
import { timeAgo } from "./time";

describe("names", () => {
  it("addresses people by first name, without honorifics", () => {
    expect(firstName("Dr. Helen Ward")).toBe("Helen");
    expect(firstName("Prof. Maya Henderson")).toBe("Maya");
    expect(firstName("Alex Chen")).toBe("Alex");
  });

  it("takes first and last initials, or a monogram", () => {
    expect(initials("Dr. Helen Ward")).toBe("HW");
    expect(initials("Raj")).toBe("R");
    expect(initials("Northlake Characterization Core", 1)).toBe("N");
    expect(initials("  ")).toBe("?");
  });

  it("shows the title, or the role when there's none, then the org's short name", () => {
    expect(memberSubtitle(members.helen)).toBe("Core director · Northlake");
    expect(memberSubtitle({ ...members.raj, title: null })).toBe("Member · Northlake");
    expect(memberSubtitle(members.alex)).toBe("PhD student · Henderson Lab");
  });
});

describe("timeAgo", () => {
  const now = Date.parse("2026-10-26T10:30:00Z");

  it("phrases the Chats list's last-message times", () => {
    expect(timeAgo("2026-10-26T10:29:58Z", now)).toBe("just now");
    expect(timeAgo("2026-10-26T10:29:37Z", now)).toBe("23 seconds ago");
    expect(timeAgo("2026-10-26T08:30:00Z", now)).toBe("2 hours ago");
    expect(timeAgo("2026-10-25T10:00:00Z", now)).toBe("1 day ago");
  });

  it("never reads the future as the past", () => {
    expect(timeAgo("2026-10-26T11:00:00Z", now)).toBe("just now");
  });
});
