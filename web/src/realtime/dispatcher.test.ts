import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { PasscodeRequired } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import { applyEntitySnapshot, type EntityData, type Versioned } from "./cache";
import { RealtimeDispatcher, TAB_CHANNEL, eventStreamUrl, type StreamSnapshot } from "./dispatcher";
import {
  ChannelHub,
  EventSources,
  MEMBER_ID,
  SESSION_ID,
  THREAD_ID,
  demoNotice,
  durable,
  ephemeral,
  memberChanged,
  needsHuman,
  threadSnapshot,
} from "./testing";

const RETRY_MS = 1_000;

function snapshotAt(cursor: number, epoch = 1): StreamSnapshot {
  return { cursor, epoch, sessionId: SESSION_ID };
}

interface Tab {
  dispatcher: RealtimeDispatcher;
  queryClient: QueryClient;
  sources: EventSources;
  snapshot: Mock<() => Promise<StreamSnapshot | null>>;
  reload: Mock<() => void>;
  leave: Mock<(url: string) => void>;
}

/** A dispatcher whose snapshots are served in order from `snapshots`. */
function openTab(hub: ChannelHub, snapshots: (StreamSnapshot | null | Error)[]): Tab {
  const queryClient = new QueryClient();
  const sources = new EventSources();
  const queue = [...snapshots];
  const snapshot = vi.fn((): Promise<StreamSnapshot | null> => {
    const next = queue.shift();
    if (next === undefined) return Promise.reject(new Error("test served no more snapshots"));
    return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
  });
  const reload = vi.fn<() => void>();
  const leave = vi.fn<(url: string) => void>();
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot,
    openEventSource: sources.open,
    openChannel: hub.open,
    reload,
    retryDelayMs: () => RETRY_MS,
    here: () => "/network?focus=thr_1#edge",
    leave,
  });
  return { dispatcher, queryClient, sources, snapshot, reload, leave };
}

function threadEntity(queryClient: QueryClient): Versioned<EntityData> | undefined {
  return queryClient.getQueryData<Versioned<EntityData>>(queryKeys.entity("thread", THREAD_ID));
}

let hub: ChannelHub;
const running: RealtimeDispatcher[] = [];

function tab(snapshots: (StreamSnapshot | null | Error)[]): Tab {
  const opened = openTab(hub, snapshots);
  running.push(opened.dispatcher);
  return opened;
}

beforeEach(() => {
  hub = new ChannelHub();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  for (const dispatcher of running.splice(0)) dispatcher.stop();
  vi.useRealTimers();
});

describe("opening the stream", () => {
  it("names the snapshot's cursor and epoch in the stream URL", () => {
    expect(eventStreamUrl(null)).toBe("/api/events");
    expect(eventStreamUrl(null, 0)).toBe("/api/events?epoch=0");
    expect(eventStreamUrl(5, 2)).toBe("/api/events?after=5&epoch=2");
  });

  it("opens one EventSource after the snapshot's cursor", async () => {
    const { dispatcher, sources } = tab([snapshotAt(10)]);

    await dispatcher.start();
    await dispatcher.start(); // already running: no second stream

    expect(sources.opened.map((source) => source.url)).toEqual(["/api/events?after=10&epoch=1"]);
    expect(dispatcher.state).toMatchObject({
      status: "connecting",
      generation: 1,
      epoch: 1,
      sessionId: SESSION_ID,
      lastSeq: 10,
    });
    sources.latest.accept();
    expect(dispatcher.state.status).toBe("open");
  });

  it("waits without a session, and opens once one is started", async () => {
    const { dispatcher, sources } = tab([null, snapshotAt(3)]);

    await dispatcher.start();
    expect(dispatcher.state.status).toBe("no-session");
    expect(sources.opened).toHaveLength(0);

    await dispatcher.start();
    expect(sources.latest.url).toBe("/api/events?after=3&epoch=1");
  });

  it("retries a failed snapshot with backoff", async () => {
    vi.useFakeTimers();
    const { dispatcher, sources, snapshot } = tab([new Error("502"), snapshotAt(4)]);

    await dispatcher.start();
    expect(dispatcher.state).toMatchObject({ status: "starting", failures: 1 });
    expect(sources.opened).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(RETRY_MS);
    expect(snapshot).toHaveBeenCalledTimes(2);
    expect(sources.latest.url).toBe("/api/events?after=4&epoch=1");
    expect(dispatcher.state.failures).toBe(0);
  });

  it("never opens a stream for a snapshot that arrives after stop()", async () => {
    let resolve: (snapshot: StreamSnapshot) => void = () => {};
    const sources = new EventSources();
    const dispatcher = new RealtimeDispatcher({
      queryClient: new QueryClient(),
      snapshot: () =>
        new Promise((r) => {
          resolve = r;
        }),
      openEventSource: sources.open,
      openChannel: hub.open,
    });

    const started = dispatcher.start();
    dispatcher.stop();
    resolve(snapshotAt(1));
    await started;

    expect(sources.opened).toHaveLength(0);
    expect(dispatcher.state.status).toBe("stopped");
  });
});

describe("entity snapshots", () => {
  it("applies a snapshot only when its version is newer than the cached one", async () => {
    const { dispatcher, queryClient, sources } = tab([snapshotAt(10)]);
    await dispatcher.start();
    const source = sources.latest;

    source.send(threadSnapshot(11, 2, "negotiating"));
    expect(threadEntity(queryClient)).toEqual({ version: 2, data: { state: "negotiating" } });

    source.send(threadSnapshot(12, 1, "open"));
    source.send(threadSnapshot(13, 2, "replayed"));
    expect(threadEntity(queryClient)).toEqual({ version: 2, data: { state: "negotiating" } });

    source.send(threadSnapshot(14, 3, "agreed"));
    expect(threadEntity(queryClient)).toEqual({ version: 3, data: { state: "agreed" } });
  });

  it("keeps a newer version fetched over REST when an older event arrives", async () => {
    const { dispatcher, queryClient, sources } = tab([snapshotAt(10)]);
    await dispatcher.start();
    queryClient.setQueryData(queryKeys.entity("thread", THREAD_ID), {
      version: 5,
      data: { state: "agreed" },
    });

    sources.latest.send(threadSnapshot(11, 4, "negotiating"));

    expect(threadEntity(queryClient)).toEqual({ version: 5, data: { state: "agreed" } });
  });

  it("marks the inbox list stale only when a newer thread snapshot is applied", () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(queryKeys.threads, []);
    queryClient.setQueryData(queryKeys.entity("thread", THREAD_ID), { version: 3, data: {} });
    const stale = threadSnapshot(1, 2, "open").event;
    const newer = threadSnapshot(2, 4, "agreed").event;
    if (stale.type !== "entity.snapshot" || newer.type !== "entity.snapshot") throw new Error();

    expect(applyEntitySnapshot(queryClient, stale)).toBe(false);
    expect(queryClient.getQueryState(queryKeys.threads)?.isInvalidated).toBe(false);

    expect(applyEntitySnapshot(queryClient, newer)).toBe(true);
    expect(queryClient.getQueryState(queryKeys.threads)?.isInvalidated).toBe(true);
  });
});

describe("session.member_changed", () => {
  it("closes the stream, swaps the query cache and reopens from a fresh cursor", async () => {
    const queryClient = new QueryClient();
    const sources = new EventSources();
    let resolveSecond: (snapshot: StreamSnapshot) => void = () => {};
    const snapshot = vi
      .fn<() => Promise<StreamSnapshot | null>>()
      .mockResolvedValueOnce(snapshotAt(10, 1))
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveSecond = resolve;
        }),
      );
    const dispatcher = new RealtimeDispatcher({
      queryClient,
      snapshot,
      openEventSource: sources.open,
      openChannel: hub.open,
      reload: vi.fn(),
    });
    running.push(dispatcher);
    await dispatcher.start();
    const first = sources.latest;
    first.accept();
    first.send(threadSnapshot(11, 1, "open"));
    queryClient.setQueryData(queryKeys.threads, [{ id: THREAD_ID }]);

    first.send(memberChanged(12, 2));

    // Synchronously the old stream is closed, but the previous member's cache stays up
    // while the snapshot is in flight, so the frame isn't blanked.
    expect(first.closed).toBe(true);
    expect(dispatcher.state).toMatchObject({ status: "resetting", generation: 1 });
    expect(queryClient.getQueryData(queryKeys.threads)).toEqual([{ id: THREAD_ID }]);
    // Something the old frame fetches during the switch...
    queryClient.setQueryData(queryKeys.inbox, { stale: true });

    resolveSecond({
      ...snapshotAt(25, 2),
      prime: () => {
        queryClient.setQueryData(queryKeys.session, { epoch: 2 });
      },
    });
    await vi.waitFor(() => {
      expect(sources.opened).toHaveLength(2);
    });

    // ...is gone with the rest, and only what the snapshot primed remains.
    expect(queryClient.getQueryCache().getAll().map((query) => query.queryKey)).toEqual([queryKeys.session]);
    expect(snapshot).toHaveBeenCalledTimes(2);
    expect(sources.latest.url).toBe("/api/events?after=25&epoch=2");
    expect(dispatcher.state).toMatchObject({ generation: 2, epoch: 2, lastSeq: 25 });
  });

  it("stops the old frame's queries, so nothing it still runs fetches as the new member", async () => {
    const { dispatcher, queryClient, sources } = tab([snapshotAt(10, 1), snapshotAt(25, 2)]);
    await dispatcher.start();
    // The previous member's chat, on screen.
    const fetchChat = vi.fn(() => Promise.resolve({ id: "chat_1" }));
    const chat = { queryKey: ["conversation", "chat_1"], queryFn: fetchChat };
    const observer = new QueryObserver(queryClient, chat);
    const unsubscribe = observer.subscribe(() => {});
    await vi.waitFor(() => {
      expect(queryClient.getQueryData(chat.queryKey)).toEqual({ id: "chat_1" });
    });

    sources.latest.send(memberChanged(11, 2));
    // While the snapshot is in flight, the frame still shows the chat...
    expect(observer.getCurrentResult().data).toEqual({ id: "chat_1" });
    await vi.waitFor(() => {
      expect(dispatcher.state.generation).toBe(2);
    });
    // ...and an effect it left pending (useQuery's setOptions) runs once the cache is cleared.
    observer.setOptions(chat);

    expect(fetchChat).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it("primes the cache from the snapshot before publishing the generation", async () => {
    const queryClient = new QueryClient();
    const seen: unknown[] = [];
    const dispatcher = new RealtimeDispatcher({
      queryClient,
      snapshot: () =>
        Promise.resolve({
          ...snapshotAt(3),
          prime: () => {
            queryClient.setQueryData(queryKeys.session, { epoch: 1 });
          },
        }),
      openEventSource: new EventSources().open,
      openChannel: hub.open,
    });
    running.push(dispatcher);
    dispatcher.store.subscribe((state, previous) => {
      if (state.generation !== previous.generation) seen.push(queryClient.getQueryData(queryKeys.session));
    });

    await dispatcher.start();

    expect(seen).toEqual([{ epoch: 1 }]);
  });

  it("handles each switch once, whether act-as or the stream reports it first", async () => {
    const { dispatcher, sources, snapshot } = tab([snapshotAt(10, 1), snapshotAt(25, 2)]);
    await dispatcher.start();

    await dispatcher.memberChanged(2); // the act-as request in this tab succeeded
    sources.latest.send(memberChanged(26, 2)); // then its event arrives anyway
    await dispatcher.memberChanged(1); // and a stale epoch is ignored too

    expect(snapshot).toHaveBeenCalledTimes(2);
    expect(sources.opened).toHaveLength(2);
    expect(sources.latest.closed).toBe(false);
  });

  it("ignores a member change for a different session", async () => {
    const { dispatcher, sources } = tab([snapshotAt(10, 1)]);
    await dispatcher.start();

    sources.latest.send(memberChanged(11, 2, "6f1c2c1e-0000-4000-8000-0000000000ff"));

    expect(sources.latest.closed).toBe(false);
    expect(dispatcher.state.epoch).toBe(1);
  });
});

describe("demo notices", () => {
  it("go to demo subscribers and never touch the query cache", async () => {
    const { dispatcher, queryClient, sources } = tab([snapshotAt(10)]);
    await dispatcher.start();
    queryClient.setQueryData(queryKeys.threads, [{ id: THREAD_ID }]);
    queryClient.setQueryData(queryKeys.entity("thread", THREAD_ID), { version: 1, data: {} });
    const before = queryClient.getQueryCache().getAll().map((query) => [query.queryHash, query.state.data]);
    const writes = [
      vi.spyOn(queryClient, "setQueryData"),
      vi.spyOn(queryClient, "setQueriesData"),
      vi.spyOn(queryClient, "invalidateQueries"),
      vi.spyOn(queryClient, "removeQueries"),
      vi.spyOn(queryClient, "resetQueries"),
      vi.spyOn(queryClient, "refetchQueries"),
      vi.spyOn(queryClient, "clear"),
    ];
    const failing = vi.fn(() => {
      throw new Error("a broken toast");
    });
    const received = vi.fn();
    vi.spyOn(console, "error").mockImplementation(() => {});
    dispatcher.onDemo(failing);
    dispatcher.onDemo(received);

    const notice = demoNotice(11, "escalation.needs_human");
    sources.latest.send(notice);

    expect(received).toHaveBeenCalledExactlyOnceWith(notice.event, notice);
    expect(failing).toHaveBeenCalledOnce();
    for (const write of writes) expect(write).not.toHaveBeenCalled();
    const after = queryClient.getQueryCache().getAll().map((query) => [query.queryHash, query.state.data]);
    expect(after).toEqual(before);
    expect(dispatcher.state.lastSeq).toBe(11);
  });
});

describe("needs-human cues", () => {
  it("reach demo subscribers like any demo notice, and leave the cache alone", async () => {
    const { dispatcher, queryClient, sources } = tab([snapshotAt(10)]);
    await dispatcher.start();
    const received = vi.fn();
    dispatcher.onDemo(received);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    const cue = needsHuman(11, "helen", "6f1c2c1e-0000-4000-8000-0000000000e1");
    sources.latest.send(cue);

    expect(received).toHaveBeenCalledExactlyOnceWith(cue.event, cue);
    expect(invalidate).not.toHaveBeenCalled();
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });
});

describe("other tabs", () => {
  const OTHER_SESSION = "6f1c2c1e-0000-4000-8000-0000000000ab";

  it("follow an account switch in one tab in place, with no reload", async () => {
    const a = tab([snapshotAt(10, 1), snapshotAt(20, 2)]);
    const b = tab([snapshotAt(10, 1), snapshotAt(20, 2)]);
    await a.dispatcher.start();
    await b.dispatcher.start();
    const heard = vi.fn();
    b.dispatcher.onMemberChanged(heard);
    const before = b.sources.latest;

    a.sources.latest.send(memberChanged(11, 2));

    expect(heard).toHaveBeenCalledExactlyOnceWith({ epoch: 2, memberId: MEMBER_ID });
    expect(before.closed).toBe(true);
    await vi.waitFor(() => {
      expect(b.sources.latest.url).toBe("/api/events?after=20&epoch=2");
    });
    expect(b.dispatcher.state).toMatchObject({ generation: 2, epoch: 2, sessionId: SESSION_ID });
    expect(b.reload).not.toHaveBeenCalled();
    expect(a.reload).not.toHaveBeenCalled();
  });

  it("follow each switch once, whichever tab hears of it first", async () => {
    const a = tab([snapshotAt(10, 1), snapshotAt(20, 2)]);
    const b = tab([snapshotAt(10, 1), snapshotAt(20, 2)]);
    await a.dispatcher.start();
    await b.dispatcher.start();
    const heardA = vi.fn();
    const heardB = vi.fn();
    a.dispatcher.onMemberChanged(heardA);
    b.dispatcher.onMemberChanged(heardB);
    const streamA = a.sources.latest;

    b.sources.latest.send(memberChanged(11, 2)); // b hears it first, and tells a
    streamA.send(memberChanged(11, 2)); // a's own (now closed) stream reports it later
    await a.dispatcher.memberChanged(2, { sessionId: SESSION_ID, memberId: MEMBER_ID }); // then its act-as returns

    expect(heardA).toHaveBeenCalledOnce();
    expect(heardB).toHaveBeenCalledOnce();
    expect(a.snapshot).toHaveBeenCalledTimes(2);
    expect(b.snapshot).toHaveBeenCalledTimes(2);
  });

  it("tell each other which session and member a switch was to", async () => {
    const { dispatcher, sources } = tab([snapshotAt(10, 1), snapshotAt(20, 2)]);
    await dispatcher.start();
    const other = vi.fn();
    hub.open(TAB_CHANNEL).onmessage = other;

    sources.latest.send(memberChanged(11, 2));

    expect(other).toHaveBeenCalledOnce();
    expect((other.mock.calls[0]?.[0] as MessageEvent).data).toEqual({
      type: "member_changed",
      sessionId: SESSION_ID,
      epoch: 2,
      memberId: MEMBER_ID,
    });
  });

  it("rebuild a tab in another session from a fresh snapshot, whatever its epoch", async () => {
    const { dispatcher, sources, snapshot } = tab([
      snapshotAt(10, 5),
      { cursor: 3, epoch: 1, sessionId: OTHER_SESSION },
    ]);
    await dispatcher.start();

    hub.open(TAB_CHANNEL).postMessage({
      type: "member_changed",
      sessionId: OTHER_SESSION,
      epoch: 1,
      memberId: MEMBER_ID,
    });

    await vi.waitFor(() => {
      expect(sources.opened).toHaveLength(2);
    });
    expect(snapshot).toHaveBeenCalledTimes(2);
    expect(sources.latest.url).toBe("/api/events?after=3&epoch=1");
    expect(dispatcher.state).toMatchObject({ sessionId: OTHER_SESSION, epoch: 1 });
  });

  it("forget the epoch when the session is gone, so a new session's first switch is followed", async () => {
    vi.useFakeTimers();
    const { dispatcher, sources } = tab([
      snapshotAt(10, 5),
      null, // the stream was refused: the session was wiped
      null,
      { cursor: 4, epoch: 1, sessionId: OTHER_SESSION },
    ]);
    await dispatcher.start();
    sources.latest.fail();
    await vi.advanceTimersByTimeAsync(RETRY_MS);
    expect(dispatcher.state).toMatchObject({ status: "no-session", sessionId: null, epoch: null });

    // Another tab starts a new session and makes its first switch, to epoch 1.
    hub.open(TAB_CHANNEL).postMessage({
      type: "member_changed",
      sessionId: OTHER_SESSION,
      epoch: 1,
      memberId: MEMBER_ID,
    });
    await vi.advanceTimersByTimeAsync(0);

    expect(sources.latest.url).toBe("/api/events?after=4&epoch=1");
    expect(dispatcher.state).toMatchObject({ sessionId: OTHER_SESSION, epoch: 1 });
  });

  it("ignore unrelated messages on the channel", async () => {
    const { dispatcher, snapshot, reload } = tab([snapshotAt(10, 1)]);
    await dispatcher.start();

    const other = hub.open(TAB_CHANNEL);
    other.postMessage({ type: "something_else", epoch: 9 });
    other.postMessage({ type: "member_changed", epoch: 9 }); // no session
    other.postMessage({ type: "member_changed", sessionId: SESSION_ID, epoch: 1, memberId: MEMBER_ID }); // seen
    other.postMessage("member_changed");

    expect(snapshot).toHaveBeenCalledOnce();
    expect(reload).not.toHaveBeenCalled();
  });

  it("reach each other through the platform BroadcastChannel", async () => {
    const make = (snapshots: StreamSnapshot[]) => {
      const sources = new EventSources();
      const snapshot = vi.fn(() => Promise.resolve(snapshots.shift() ?? snapshotAt(99, 9)));
      const dispatcher = new RealtimeDispatcher({
        queryClient: new QueryClient(),
        snapshot,
        openEventSource: sources.open,
      });
      running.push(dispatcher);
      return { dispatcher, sources, snapshot };
    };
    const a = make([snapshotAt(10, 1), snapshotAt(20, 2)]);
    const b = make([snapshotAt(10, 1), snapshotAt(20, 2)]);
    await a.dispatcher.start();
    await b.dispatcher.start();

    a.sources.latest.send(memberChanged(11, 2));

    await vi.waitFor(() => {
      expect(b.sources.latest.url).toBe("/api/events?after=20&epoch=2");
    });
    expect(b.snapshot).toHaveBeenCalledTimes(2);
  });
});

describe("connection loss", () => {
  it("leaves reconnection to the browser while it is retrying", async () => {
    const { dispatcher, sources } = tab([snapshotAt(10)]);
    await dispatcher.start();
    sources.latest.accept();

    sources.latest.drop();
    expect(dispatcher.state.status).toBe("reconnecting");
    expect(sources.opened).toHaveLength(1);

    sources.latest.accept();
    expect(dispatcher.state.status).toBe("open");
  });

  it("reopens after the last applied seq when the browser gives up", async () => {
    vi.useFakeTimers();
    // The second snapshot only confirms the session; its cursor is not used.
    const { dispatcher, queryClient, sources, snapshot } = tab([snapshotAt(10), snapshotAt(30)]);
    await dispatcher.start();
    sources.latest.accept();
    sources.latest.send(threadSnapshot(11, 1, "open"));
    sources.latest.send(threadSnapshot(12, 2, "negotiating"));

    sources.latest.fail();
    expect(dispatcher.state.status).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(RETRY_MS);

    expect(sources.opened.map((source) => source.url)).toEqual([
      "/api/events?after=10&epoch=1",
      "/api/events?after=12&epoch=1",
    ]);
    expect(snapshot).toHaveBeenCalledTimes(2);
    expect(dispatcher.state).toMatchObject({ generation: 1, lastSeq: 12 }); // cache kept
    expect(threadEntity(queryClient)?.version).toBe(2);
  });

  it("rebuilds when the stream was refused for a session that moved on", async () => {
    vi.useFakeTimers();
    // A reset in another tab: the epoch moved with no event this stream could see, so
    // the server ended it and refused the browser's reconnect to the stale epoch.
    const { dispatcher, queryClient, sources, snapshot } = tab([
      snapshotAt(10, 1),
      snapshotAt(4, 2),
      snapshotAt(4, 2),
    ]);
    await dispatcher.start();
    sources.latest.accept();
    sources.latest.send(threadSnapshot(11, 1, "open"));
    const otherTab = vi.fn();
    hub.open(TAB_CHANNEL).onmessage = otherTab;

    sources.latest.fail();
    await vi.advanceTimersByTimeAsync(RETRY_MS);

    expect(snapshot).toHaveBeenCalledTimes(3);
    expect(sources.latest.url).toBe("/api/events?after=4&epoch=2");
    expect(dispatcher.state).toMatchObject({ generation: 2, epoch: 2, lastSeq: 4 });
    expect(threadEntity(queryClient)).toBeUndefined();
    expect(otherTab).toHaveBeenCalledOnce();
  });

  it("clears everything when the session is gone", async () => {
    vi.useFakeTimers();
    const { dispatcher, queryClient, sources } = tab([snapshotAt(10), null, null]);
    await dispatcher.start();
    sources.latest.send(threadSnapshot(11, 1, "open"));

    sources.latest.fail();
    await vi.advanceTimersByTimeAsync(RETRY_MS);

    expect(sources.opened).toHaveLength(1);
    expect(dispatcher.state.status).toBe("no-session");
    expect(threadEntity(queryClient)).toBeUndefined();
  });

  it("keeps checking the session while snapshots fail", async () => {
    vi.useFakeTimers();
    const { dispatcher, sources, snapshot } = tab([snapshotAt(10), new Error("502"), snapshotAt(10)]);
    await dispatcher.start();

    sources.latest.fail();
    await vi.advanceTimersByTimeAsync(RETRY_MS);
    expect(sources.opened).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(RETRY_MS);

    expect(snapshot).toHaveBeenCalledTimes(3);
    expect(sources.latest.url).toBe("/api/events?after=10&epoch=1");
  });

  it("skips durable events it has already applied", async () => {
    const { dispatcher, sources } = tab([snapshotAt(10)]);
    await dispatcher.start();
    const seen = vi.fn();
    dispatcher.onEnvelope(seen);

    sources.latest.send(threadSnapshot(10, 1, "open")); // at the cursor: in the snapshot
    sources.latest.send(threadSnapshot(11, 1, "open"));
    sources.latest.send(threadSnapshot(11, 1, "open"));
    sources.latest.sendRaw("not json");

    expect(seen).toHaveBeenCalledOnce();
    expect(dispatcher.state.lastSeq).toBe(11);
  });
});

describe("other events", () => {
  it("sends stream events to stream subscribers without moving the cursor", async () => {
    const { dispatcher, queryClient, sources } = tab([snapshotAt(10)]);
    await dispatcher.start();
    const received = vi.fn();
    dispatcher.onStream(received);

    const delta = ephemeral({
      type: "stream.delta",
      org_id: "6f1c2c1e-0000-4000-8000-0000000000c1",
      thread_id: THREAD_ID,
      turn_id: "6f1c2c1e-0000-4000-8000-0000000000e1",
      attempt: 1,
      text: "Checking the CT calendar",
    });
    sources.latest.send(delta);

    expect(received).toHaveBeenCalledExactlyOnceWith(delta.event, delta);
    expect(dispatcher.state.lastSeq).toBe(10);
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });

  it("rebuilds from a fresh snapshot when its world is retired", async () => {
    const { dispatcher, sources, snapshot } = tab([snapshotAt(10), snapshotAt(3)]);
    await dispatcher.start();
    const first = sources.latest;
    const otherTab = vi.fn();
    hub.open(TAB_CHANNEL).onmessage = otherTab;

    first.send(durable(11, { type: "world.retired" }));

    expect(first.closed).toBe(true);
    await vi.waitFor(() => {
      expect(sources.opened).toHaveLength(2);
    });
    expect(snapshot).toHaveBeenCalledTimes(2);
    expect(sources.latest.url).toBe("/api/events?after=3&epoch=1");
    expect(otherTab).not.toHaveBeenCalled(); // each tab sees world.retired on its own stream
  });
});

describe("the passcode gate", () => {
  const GATE = "/api/gate?next=%2Fnetwork%3Ffocus%3Dthr_1%23edge";

  it("locks on a PasscodeRequired snapshot: no retry, no stream, off to the passcode page", async () => {
    vi.useFakeTimers();
    const { dispatcher, sources, snapshot, leave } = tab([new PasscodeRequired()]);

    await dispatcher.start();

    expect(dispatcher.state).toMatchObject({ status: "locked", failures: 0, generation: 0 });
    expect(vi.getTimerCount()).toBe(0);
    expect(sources.opened).toHaveLength(0);
    expect(leave).toHaveBeenCalledExactlyOnceWith(GATE);

    await vi.advanceTimersByTimeAsync(RETRY_MS * 10);
    await dispatcher.start(); // locked is terminal
    expect(snapshot).toHaveBeenCalledOnce();
  });

  it("still waits on the landing when there is simply no session", async () => {
    const { dispatcher, leave } = tab([null]);

    await dispatcher.start();

    expect(dispatcher.state.status).toBe("no-session");
    expect(leave).not.toHaveBeenCalled();
  });

  it("locks when a refused stream's recovery snapshot meets the gate, rather than resetting", async () => {
    vi.useFakeTimers();
    const { dispatcher, queryClient, sources, snapshot, leave } = tab([snapshotAt(10), new PasscodeRequired()]);
    await dispatcher.start();
    sources.latest.accept();
    sources.latest.send(threadSnapshot(11, 1, "open"));

    sources.latest.fail();
    await vi.advanceTimersByTimeAsync(RETRY_MS);
    await vi.advanceTimersByTimeAsync(RETRY_MS * 10);

    expect(dispatcher.state).toMatchObject({ status: "locked", failures: 0 });
    expect(snapshot).toHaveBeenCalledTimes(2);
    expect(sources.opened).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
    expect(threadEntity(queryClient)).toBeUndefined();
    expect(leave).toHaveBeenCalledExactlyOnceWith(GATE);
  });

  it("clears the cache and notifies before telling other tabs, then leaves", async () => {
    const { dispatcher, queryClient, sources, leave } = tab([snapshotAt(10)]);
    await dispatcher.start();
    sources.latest.send(threadSnapshot(11, 1, "open"));
    const order: string[] = [];
    dispatcher.onLocked(() => {
      order.push(threadEntity(queryClient) === undefined ? "listener after clear" : "listener before clear");
    });
    hub.open(TAB_CHANNEL).onmessage = (event: MessageEvent) => {
      order.push(`posted ${JSON.stringify(event.data)}`);
    };
    leave.mockImplementation(() => order.push("left"));

    dispatcher.lock();
    dispatcher.lock(); // idempotent

    expect(order).toEqual(["listener after clear", 'posted {"type":"locked"}', "left"]);
    expect(sources.latest.closed).toBe(true);
  });

  it("reloads the visitor's other tabs, which ignore anything after", async () => {
    const a = tab([snapshotAt(10, 1)]);
    const b = tab([snapshotAt(10, 1)]);
    await a.dispatcher.start();
    await b.dispatcher.start();

    a.dispatcher.lock();

    expect(b.reload).toHaveBeenCalledOnce();
    expect(a.reload).not.toHaveBeenCalled();
    b.dispatcher.lock();
    expect(a.reload).not.toHaveBeenCalled(); // already locked
  });

  it("ignores member changes and resyncs once locked", async () => {
    const { dispatcher, sources, snapshot } = tab([snapshotAt(10, 1)]);
    await dispatcher.start();
    dispatcher.lock();

    await dispatcher.memberChanged(2);
    await dispatcher.resync();

    expect(snapshot).toHaveBeenCalledOnce();
    expect(sources.opened).toHaveLength(1);
    expect(dispatcher.state.status).toBe("locked");
  });
});
