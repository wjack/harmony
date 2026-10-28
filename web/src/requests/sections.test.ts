import { describe, expect, it } from "vitest";

import { THREAD, inboxFor, requestsWorld } from "../fixtures/requests";
import { alexInbox, inbox, members } from "../fixtures/shell";
import { matches, requestPath, threadPath } from "./sections";
import type { InboxItem } from "./types";

const NOW = Date.parse("2026-10-26T10:30:00Z");
const helens = inboxFor(requestsWorld(NOW), members.helen);

function first(items: readonly InboxItem[]): InboxItem {
  const [item] = items;
  if (item === undefined) throw new Error("no inbox item");
  return item;
}

describe("matches", () => {
  const henderson = first(helens.needs_you.filter((item) => item.thread_id === THREAD.henderson));

  it("finds a request by its title or its counterparty, ignoring case", () => {
    expect(matches(henderson, "sem/eds", "all")).toBe(true);
    expect(matches(henderson, "  henderson ", "all")).toBe(true);
    expect(matches(henderson, "PEEK", "all")).toBe(false);
  });

  it("filters by direction: inbound is ours to answer, outbound is ours to ask", () => {
    expect(matches(henderson, "", "inbound")).toBe(true);
    expect(matches(henderson, "", "outbound")).toBe(false);
    const asked = first(alexInbox.needs_you);
    expect(matches(asked, "", "outbound")).toBe(true);
    expect(matches(asked, "", "inbound")).toBe(false);
  });
});

describe("requestPath", () => {
  it("opens an item waiting on the viewer where it's answered, and every other row at its thread", () => {
    const henderson = first(inbox.needs_you);
    expect(requestPath(henderson, "needs_you")).toBe(`/requests/${henderson.thread_id}`);
    // A question our own representative asked is answered in the chat that sent the request.
    const asked = first(alexInbox.needs_you);
    expect(requestPath(asked, "needs_you")).toBe(`/chat/${asked.conversation_id ?? ""}`);
    expect(requestPath(asked, "done")).toBe(threadPath(asked));
    expect(threadPath(asked)).toBe(`/requests/${asked.thread_id}`);
  });
});
