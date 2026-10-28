import { describe, expect, it } from "vitest";

import type { EntitySnapshot, EventEnvelope } from "../api/types";
import {
  brief,
  cardRecipients,
  chatDeltaEnvelope,
  conversationAt,
  CONVERSATION_ID,
  initialState,
  NORTHLAKE_GO_AHEAD_ID,
  NORTHLAKE_THREAD_ID,
  OUTREACH_CARD_ID,
  REDRAFT_CARD_ID,
  REDRAFT_MESSAGE,
  redraftBrief,
  REQUEST_ID,
  RIDGELINE_GO_AHEAD_ID,
  TUBE_QUESTION_ID,
  SENT_REPLY,
  TURN,
} from "../fixtures/chat";
import { WORLD_ID } from "../fixtures/shell";
import { summarize } from "./activity";
import { editedText, screenedLine } from "./brief";
import { realCalendar } from "../demo/worldClock";
import { formatSince } from "./format";
import { committedSteps, pendingCard, settledMessages, toBlocks, wasReplaced, type OptimisticMessage } from "./items";
import { chatDeltaOf, concernsConversation, concernsRequest, watchedIds } from "./live";
import {
  approveBody,
  BRIEF_PART,
  hasClearedPart,
  hasEdits,
  initialDraft,
  outreachTitle,
  partVersions,
  showVersion,
  shownVersion,
} from "./outreachDraft";
import { outreachRecipients } from "./recipients";
import { applyDelta, longestOverlap, visibleText, type CommittedStep } from "./stream";
import type { CardRecipient, OutreachCardItem, ToolActivityItem, UserTextItem } from "./types";
import { staysPinned } from "./useStickToBottom";

describe("toBlocks", () => {
  it("collapses a turn's tool steps and thinking into one activity line", () => {
    const blocks = toBlocks(conversationAt(initialState("drafted")).items);
    expect(blocks.map((block) => block.kind)).toEqual(["user", "activity", "assistant", "outreach"]);
    const activity = blocks[1];
    expect(activity?.kind === "activity" && activity.items.map((item) => item.type)).toEqual([
      "thinking_summary",
      "tool_activity",
      "tool_activity",
      "tool_activity",
    ]);
  });

  it("starts a new activity line for a new turn", () => {
    const kinds = toBlocks(conversationAt(initialState("closed")).items).map((block) => block.kind);
    expect(kinds).toEqual([
      "user",
      "activity",
      "assistant",
      "outreach",
      "request",
      "assistant",
      "question",
      "question",
      "question",
      "activity",
      "assistant",
    ]);
  });

  it("folds a draft a message replaced to an earlier version, under the message that asked for it", () => {
    const items = conversationAt(initialState("drafted", { rejected: { with: "message", text: REDRAFT_MESSAGE } })).items;
    const blocks = toBlocks(items);
    expect(blocks.slice(3).map((block) => block.kind)).toEqual(["earlier", "user", "assistant", "outreach"]);
    const message = blocks[4];
    expect(message?.kind === "user" && message.item.text).toBe(REDRAFT_MESSAGE);
  });

  it("keeps a declined card as its own record, which nothing replaced", () => {
    const declined = { with: "decline", text: null } as const;
    const blocks = toBlocks(conversationAt(initialState("drafted", { rejected: declined })).items);
    expect(blocks.at(-1)?.kind).toBe("outreach");
    expect(blocks.some((block) => block.kind === "earlier")).toBe(false);
  });

  it("puts the reply actions on the last text before each member message", () => {
    const ends = (stage: Parameters<typeof initialState>[0]) =>
      toBlocks(conversationAt(initialState(stage)).items).flatMap((block) =>
        block.kind === "assistant" && block.endOfReply ? [block.item.id] : [],
      );
    expect(ends("sent")).toEqual([`turn:${TURN.send}:0`]);
    expect(ends("closed")).toEqual([`turn:${TURN.summary}:0`]);
  });
});

describe("pendingCard", () => {
  it("finds the outreach card the conversation waits on", () => {
    expect(pendingCard(conversationAt(initialState("drafted")))?.card_id).toBe(OUTREACH_CARD_ID);
  });

  it("is null once the card is answered", () => {
    expect(pendingCard(conversationAt(initialState("sent")))).toBeNull();
    expect(pendingCard(conversationAt(initialState("drafted", { rejected: { with: "decline", text: "Not now" } })))).toBeNull();
  });

  it("finds Claude's redraft once a message replaced the first card", () => {
    const detail = conversationAt(initialState("drafted", { rejected: { with: "message", text: REDRAFT_MESSAGE } }));
    expect(pendingCard(detail)?.card_id).toBe(REDRAFT_CARD_ID);
  });

  it("never asks for review of a card the chat is sending without review", () => {
    const detail = conversationAt(initialState("drafted", { auto: true, override: true }));
    expect(detail.pending_card_id).toBeNull();
    expect(detail.items.some((item) => item.type === "outreach_card" && item.status === "pending" && item.auto)).toBe(true);
    expect(pendingCard(detail)).toBeNull();
  });

  it("skips an auto card even if the conversation still names it", () => {
    const detail = conversationAt(initialState("drafted", { auto: true }));
    expect(pendingCard({ ...detail, pending_card_id: OUTREACH_CARD_ID })).toBeNull();
  });
});

describe("the outreach card's ticks", () => {
  const cardWith = (rows: CardRecipient[]): OutreachCardItem => {
    const card = conversationAt(initialState("drafted")).items.find((item) => item.type === "outreach_card");
    if (card?.type !== "outreach_card") throw new Error("no outreach card in the fixture");
    return { ...card, recipients: rows };
  };
  const ids = (set: ReadonlySet<string>) => [...set].sort();
  const [northlake, ridgeline, calder] = cardRecipients as [CardRecipient, CardRecipient, CardRecipient];

  it("lists every row and ticks the suggested ones", () => {
    const { listed, proposed } = outreachRecipients(cardWith(cardRecipients));
    expect(ids(listed)).toEqual(ids(new Set(cardRecipients.map((row) => row.org.id))));
    expect(ids(proposed)).toEqual(ids(new Set([northlake.org.id, ridgeline.org.id])));
  });

  it("falls back to the fit, then to every row, on a card without suggestions", () => {
    const unmarked = cardRecipients.map((row) => ({ ...row, suggested: null }));
    expect(ids(outreachRecipients(cardWith(unmarked)).proposed)).toEqual(ids(new Set([northlake.org.id, ridgeline.org.id])));
    const misses = unmarked.map((row) => ({ ...row, fit: "no" as const }));
    expect(outreachRecipients(cardWith(misses)).proposed.size).toBe(3);
  });

  it("starts a suggested row unticked when it says suggested: false, whatever its fit", () => {
    const card = cardWith([northlake, { ...ridgeline, suggested: false }]);
    expect([...initialDraft(card).chosen]).toEqual([northlake.org.id]);
  });

  it("always names the recipients, in the card's order", () => {
    const card = cardWith(cardRecipients);
    expect(approveBody(card, initialDraft(card))).toEqual({ recipients: [northlake.org.id, ridgeline.org.id] });
    const all = { ...initialDraft(card), chosen: new Set([calder.org.id, ridgeline.org.id, northlake.org.id]) };
    expect(approveBody(card, all)).toEqual({ recipients: [northlake.org.id, ridgeline.org.id, calder.org.id] });
  });

  it("sends only the asks that changed, and only for the ticked organizations", () => {
    const card = cardWith(cardRecipients);
    const draft = {
      ...initialDraft(card),
      asks: { [northlake.org.id]: "Quote the CT only.", [ridgeline.org.id]: `${ridgeline.ask ?? ""} `, [calder.org.id]: "Hi" },
    };
    expect(approveBody(card, draft)).toEqual({
      recipients: [northlake.org.id, ridgeline.org.id],
      asks: { [northlake.org.id]: "Quote the CT only." },
    });
    expect(hasEdits(card, draft)).toBe(true);
    expect(hasEdits(card, initialDraft(card))).toBe(false);
  });

  it("titles the card by the ticked set", () => {
    const card = cardWith(cardRecipients);
    expect(outreachTitle(initialDraft(card))).toBe("Send this request to 2 organizations?");
    expect(outreachTitle({ ...initialDraft(card), chosen: new Set([calder.org.id]) })).toBe(
      "Send this request to 1 organization?",
    );
  });
});

describe("a redraft", () => {
  const items = conversationAt(initialState("drafted", { rejected: { with: "message", text: REDRAFT_MESSAGE } })).items;
  const cards = items.filter((item): item is OutreachCardItem => item.type === "outreach_card");
  const [first, redraft] = cards as [OutreachCardItem, OutreachCardItem];
  const [northlake, , calder] = cardRecipients as [CardRecipient, CardRecipient, CardRecipient];

  it("tells a card a message replaced from a declined one, by how it was rejected", () => {
    expect(wasReplaced(first)).toBe(true);
    expect(wasReplaced({ ...first, rejected_with: "decline" })).toBe(false);
    expect(wasReplaced({ ...first, status: "superseded", rejected_with: null })).toBe(true);
  });

  it("keeps each part's wordings oldest first, counting a part Claude didn't change once", () => {
    const versions = partVersions(items, redraft);
    expect(versions.brief).toEqual([brief, redraftBrief]);
    expect(versions.asks[northlake.org.id]).toHaveLength(2);
    expect(versions.asks[calder.org.id]).toEqual([calder.ask]);
    // The first card had no earlier draft.
    expect(partVersions(items, first).brief).toEqual([brief]);
  });

  it("pages a part to an earlier wording, which then goes as the member's edit", () => {
    const versions = partVersions(items, redraft);
    const paged = showVersion(initialDraft(redraft), versions, BRIEF_PART, 0);
    expect(shownVersion(versions.brief, paged, BRIEF_PART)).toEqual({ index: 0, text: brief });
    expect(paged.brief).toBe(brief);
    expect(approveBody(redraft, paged).brief).toBe(brief.trim());
    expect(shownVersion(versions.brief, initialDraft(redraft), BRIEF_PART).index).toBe(1);
  });

  it("holds the send while the brief, or a ticked organization's ask, is cleared", () => {
    expect(hasClearedPart(redraft, initialDraft(redraft))).toBe(false);
    expect(hasClearedPart(redraft, { ...initialDraft(redraft), brief: " " })).toBe(true);
    expect(hasClearedPart(redraft, { ...initialDraft(redraft), asks: { [northlake.org.id]: "" } })).toBe(true);
    // Calder isn't ticked: its ask doesn't go out, so clearing it holds nothing.
    expect(hasClearedPart(redraft, { ...initialDraft(redraft), asks: { [calder.org.id]: "" } })).toBe(false);
  });
});

describe("settledMessages", () => {
  const items = conversationAt(initialState("drafted")).items;
  const first = items[0];
  const message = (messageId: string, text: string, after: string | null = null): OptimisticMessage => ({
    messageId,
    text,
    at: "2026-10-26T10:30:00Z",
    after,
  });

  it("settles a message once the turn that drained it stores its text", () => {
    if (first?.type !== "user_text") throw new Error("fixture starts with a member message");
    expect(settledMessages(items, [message("sent", first.text)])).toEqual(new Set(["sent"]));
    expect(settledMessages(items, [message("sent", "something else")]).size).toBe(0);
  });

  it("doesn't take an earlier message with the same text for a new one", () => {
    const ok: UserTextItem = {
      id: "msg:turn-row:0",
      at: "2026-10-26T10:30:00Z",
      type: "user_text",
      text: "ok",
      member_id: null,
    };
    expect(settledMessages([ok], [message("A", "ok"), message("B", "ok", ok.id)])).toEqual(new Set(["A"]));
  });

  it("settles two messages with the same text from one drained batch", () => {
    const batch: UserTextItem[] = [0, 1].map((block) => ({
      id: `msg:turn-row:${block}`,
      at: "2026-10-26T10:30:00Z",
      type: "user_text",
      text: "ok",
      member_id: null,
    }));
    expect(settledMessages(batch, [message("A", "ok"), message("B", "ok")])).toEqual(new Set(["A", "B"]));
  });

  it("doesn't settle a message still on its way", () => {
    expect(settledMessages(items, [message("new", "Can it be Nov 4?", items.at(-1)?.id ?? null)]).size).toBe(0);
  });

  describe("a message that answered a card", () => {
    const rejected = conversationAt(initialState("drafted", { rejected: { with: "message", text: "Leave Ridgeline out" } })).items;
    const cardIndex = rejected.findIndex((item) => item.type === "outreach_card");
    const before = rejected[cardIndex - 1]?.id ?? null;

    it("settles as the card's rejection when the card came after it", () => {
      expect(settledMessages(rejected, [message("m", "Leave Ridgeline out", before)])).toEqual(new Set(["m"]));
    });

    it("doesn't match a card rejected before the message was sent", () => {
      const card = rejected[cardIndex]?.id ?? null;
      expect(settledMessages(rejected, [message("m", "Leave Ridgeline out", card)]).size).toBe(0);
    });

    it("settles one message per card", () => {
      const twice = [message("m1", "Leave Ridgeline out", before), message("m2", "Leave Ridgeline out", before)];
      expect(settledMessages(rejected, twice)).toEqual(new Set(["m1"]));
    });
  });
});

describe("streaming", () => {
  const delta = (text: string, attempt = 1, turn_id = "t1") => ({ turn_id, attempt, text });
  const step = (id: string, markdown: string): CommittedStep => ({ id, markdown });

  it("shows everything received while nothing is committed", () => {
    let state = applyDelta(null, delta("Hello "), []);
    state = applyDelta(state, delta("world"), []);
    expect(visibleText(state, [])).toBe("Hello world");
  });

  it("hides what the turn has committed since the buffer began", () => {
    let state = applyDelta(null, delta("First block. "), []);
    state = applyDelta(state, delta("Second"), []);
    expect(visibleText(state, [step("s0", "First block.")])).toBe("Second");
  });

  it("ignores whitespace differences between the stream and the stored text", () => {
    const state = applyDelta(null, delta("A list:\n\n- one\n- two\nThen"), []);
    expect(visibleText(state, [step("s0", "A list:\n- one\n- two")])).toBe("Then");
  });

  it("doesn't count steps committed before the buffer began", () => {
    const state = applyDelta(null, delta("Second"), [step("s0", "First.")]);
    expect(visibleText(state, [step("s0", "First."), step("s1", "Sec")])).toBe("ond");
  });

  it("joins a step partway through when the chat opens mid-turn", () => {
    let state = applyDelta(null, delta("network first."), []);
    state = applyDelta(state, delta("Found three labs that fit."), []);
    expect(visibleText(state, [step("s0", "I'll search the network first.")])).toBe("Found three labs that fit.");
  });

  it("skips a step the items hadn't shown yet when an attempt began", () => {
    let state = applyDelta(null, delta("Let me search."), []);
    state = applyDelta(state, delta("Found three labs.", 2), []);
    expect(visibleText(state, [])).toBe("Found three labs.");
    expect(visibleText(state, [step("s0", "Let me search.")])).toBe("Found three labs.");
  });

  it("restarts the buffer for a new attempt and drops late frames from earlier ones", () => {
    let state = applyDelta(null, delta("Partial that was dropped"), []);
    state = applyDelta(state, delta("Northlake can ", 2), []);
    state = applyDelta(state, delta("stale partial", 1), []);
    state = applyDelta(state, delta("do Nov 4.", 2), []);
    expect(state.received).toBe("Northlake can do Nov 4.");
    expect(visibleText(state, [])).toBe("Northlake can do Nov 4.");
  });

  it("restarts for a new turn and drops the old turn's late frames", () => {
    let state = applyDelta(applyDelta(null, delta("Old"), []), delta("New", 1, "t2"), []);
    state = applyDelta(state, delta(" late", 1, "t1"), []);
    expect(state.turnId).toBe("t2");
    expect(state.received).toBe("New");
  });

  it("finds the longest stretch that starts one text and ends the other", () => {
    expect(longestOverlap(Array.from("cdef"), Array.from("abcd"))).toBe(2);
    expect(longestOverlap(Array.from("xyz"), Array.from("abc"))).toBe(0);
    expect(longestOverlap(Array.from("ab"), [])).toBe(0);
    expect(longestOverlap([], Array.from("ab"))).toBe(0);
  });

  it("reads committed steps per turn", () => {
    const items = conversationAt(initialState("sent")).items;
    expect(committedSteps(items, TURN.send).map((committed) => committed.markdown)).toEqual([
      SENT_REPLY,
    ]);
  });
});

describe("staysPinned", () => {
  const at = (scrollTop: number, scrollHeight = 2000) => ({ scrollTop, scrollHeight, clientHeight: 800 });

  it("pins near the bottom", () => {
    expect(staysPinned(false, 1300, at(1150), true)).toBe(true);
  });

  it("stays pinned when the view moves without the member", () => {
    // Scrolled to 1200 of 2000, but the request card loaded 180px before the event fired.
    expect(staysPinned(true, 900, at(1200, 2180), false)).toBe(true);
    // Scroll anchoring moved it up as the ask card gave way to the composer.
    expect(staysPinned(true, 1300, at(1216, 2180), false)).toBe(true);
  });

  it("lets go when the member scrolls up", () => {
    expect(staysPinned(true, 1200, at(900), true)).toBe(false);
  });

  it("stays let go until the member is back at the bottom", () => {
    expect(staysPinned(false, 900, at(1000), true)).toBe(false);
    expect(staysPinned(false, 1000, at(1150, 2180), false)).toBe(false);
    expect(staysPinned(false, 1000, at(1190), true)).toBe(true);
  });
});

describe("summarize", () => {
  const tool = (
    label: string,
    status: ToolActivityItem["status"],
    { name = "search_network", detail = null, outcome = null }: { name?: string; detail?: string | null; outcome?: string | null } = {},
  ): ToolActivityItem => ({
    id: `${label}:${detail ?? ""}`,
    at: "2026-10-26T10:00:00Z",
    type: "tool_activity",
    tool: name,
    label,
    detail,
    status,
    outcome,
    turn_id: "t",
  });
  const need = (detail: string, outcome: string | null = null) =>
    tool("Searched the Harmony network", "done", { detail, outcome });
  const records = tool("Searched your organization's knowledge", "done", { name: "search_org_knowledge", detail: "budget" });

  it("names the running step by its running label", () => {
    expect(summarize([records, tool("Searching the Harmony network…", "running")])).toEqual({
      text: "Searching the Harmony network…",
      running: true,
      failed: false,
    });
  });

  it("names the searches by their needs, with the organization's records", () => {
    const items = conversationAt(initialState("drafted")).items.slice(1, 5) as Parameters<typeof summarize>[0];
    expect(items.some((item) => item.type === "thinking_summary")).toBe(true);
    expect(summarize(items).text).toBe("Searched the network for 2 needs and your records");
    expect(summarize([need("CT"), need("CT")]).text).toBe("Searched the network twice");
    expect(summarize([need("CT"), need("A holder"), need("CT")]).text).toBe("Searched the network 3 times");
    expect(summarize([need("CT"), records]).text).toBe("Searched the network and your records");
  });

  it("says what it thought in a step without tools, unless the reply opens with the same words", () => {
    const thought = (text: string) => ({ id: "th", at: "2026-10-26T10:00:00Z", type: "thinking_summary" as const, text, turn_id: "t" });
    expect(summarize([thought("Both quotes fit the budget. Next, the totals.")]).text).toBe("Both quotes fit the budget");
    expect(
      summarize([thought("Both conversations have closed.")], "Both conversations have closed. Let me check where they ended up.")
        .text,
    ).toBe("Thought it through");
  });

  it("shows one step with its outcome, never its detail", () => {
    expect(summarize([need("CT on 12 coupons", "1 strong, 2 possible of 14")]).text).toBe(
      "Searched the Harmony network · 1 strong, 2 possible of 14",
    );
    expect(summarize([need("CT on 12 coupons")]).text).toBe("Searched the Harmony network");
  });

  it("joins the searches with the other steps, a repeated one named once with its count", () => {
    const checked = tool("Checked on the request", "done", { name: "get_request_status" });
    const read = tool("Read a document", "error", { name: "read_document" });
    expect(summarize([need("CT"), need("A holder"), records, checked, checked, read])).toEqual({
      text: "Searched the network for 2 needs and your records, checked on the request twice and 1 more step",
      running: false,
      failed: true,
    });
    expect(summarize([checked, checked, read]).text).toBe("Checked on the request twice, read a document");
  });
});

describe("the brief", () => {
  it("sends nothing when the member's version reads the same", () => {
    expect(editedText(brief, brief)).toBeNull();
    expect(editedText(brief, `\n${brief}  \n`)).toBeNull();
    expect(editedText(brief, "   ")).toBeNull();
  });

  it("sends the member's version, trimmed", () => {
    expect(editedText(brief, `${brief}\n\nResults by Nov 4 if you can. `)).toBe(
      `${brief}\n\nResults by Nov 4 if you can.`,
    );
  });

  it("describes the screening", () => {
    expect(screenedLine({ total: 14, strong: 2, possible: 4, no: 8, unknown: 0 })).toBe(
      "Screened 14 organizations: 2 strong, 4 possible",
    );
  });
});

describe("format", () => {
  it("says how long ago compactly", () => {
    const now = Date.parse("2026-10-26T10:30:00Z");
    expect(formatSince("2026-10-26T10:29:30Z", realCalendar, now)).toBe("just now");
    expect(formatSince("2026-10-26T10:18:00Z", realCalendar, now)).toBe("12m");
    expect(formatSince("2026-10-26T07:30:00Z", realCalendar, now)).toBe("3h");
  });
});

describe("live updates", () => {
  const snapshot = (entity_type: string, entity_id: string, data: EntitySnapshot["data"] = {}): EntitySnapshot => ({
    type: "entity.snapshot",
    entity_type,
    entity_id,
    version: 2,
    data,
  });

  it("watches the questions the conversation shows", () => {
    expect(watchedIds(conversationAt(initialState("go_ahead")).items)).toEqual(
      [TUBE_QUESTION_ID, NORTHLAKE_GO_AHEAD_ID, RIDGELINE_GO_AHEAD_ID].sort(),
    );
  });

  it("refetches the conversation for its own entities only", () => {
    const watched = new Set([TUBE_QUESTION_ID]);
    expect(concernsConversation(snapshot("conversation", CONVERSATION_ID), CONVERSATION_ID, watched)).toBe(true);
    expect(
      concernsConversation(snapshot("card", OUTREACH_CARD_ID, { conversation_id: CONVERSATION_ID }), CONVERSATION_ID, watched),
    ).toBe(true);
    expect(concernsConversation(snapshot("escalation", TUBE_QUESTION_ID), CONVERSATION_ID, watched)).toBe(true);
    expect(concernsConversation(snapshot("escalation", "someone-else"), CONVERSATION_ID, watched)).toBe(false);
    expect(concernsConversation(snapshot("conversation", "another"), CONVERSATION_ID, watched)).toBe(false);
  });

  it("refetches a request's status for its threads and their consults", () => {
    const threads = new Set([NORTHLAKE_THREAD_ID]);
    expect(concernsRequest(snapshot("network_request", REQUEST_ID), REQUEST_ID, threads)).toBe(true);
    expect(concernsRequest(snapshot("thread", NORTHLAKE_THREAD_ID), REQUEST_ID, threads)).toBe(true);
    expect(concernsRequest(snapshot("thread", "new", { request_id: REQUEST_ID }), REQUEST_ID, threads)).toBe(true);
    expect(concernsRequest(snapshot("consult", "c1", { thread_id: NORTHLAKE_THREAD_ID }), REQUEST_ID, threads)).toBe(true);
    expect(concernsRequest(snapshot("escalation", "e1", { thread_id: "elsewhere" }), REQUEST_ID, threads)).toBe(false);
  });

  it("reads chat deltas off envelopes and ignores other events", () => {
    expect(chatDeltaOf(chatDeltaEnvelope(TURN.search, 2, "Hi"))).toMatchObject({
      conversation_id: CONVERSATION_ID,
      turn_id: TURN.search,
      attempt: 2,
      text: "Hi",
    });
    const other: EventEnvelope = {
      world_id: WORLD_ID,
      seq: 3,
      created_at: "2026-10-26T10:30:00Z",
      scope: { kind: "world", org_id: null, member_id: null, thread_id: null, session_id: null },
      event: snapshot("conversation", CONVERSATION_ID),
    };
    expect(chatDeltaOf(other)).toBeNull();
  });
});
