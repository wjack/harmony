// @vitest-environment jsdom
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BudgetView } from "../app/budget";
import { budgets, members, sessionAs } from "../fixtures/shell";
import { demoNotice } from "../realtime/testing";
import { demoBar, FakeServer, renderShell } from "../test/shellHarness";
import { clearAllAlerts } from "./alerts";
import { demoStore } from "./store";

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  );
  demoStore.setState({ welcomed: true, controlsOpen: false, controlsTab: "overview", resetConfirmOpen: false });
  clearAllAlerts();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

async function renderWithBudget(budget: BudgetView) {
  const server = new FakeServer(sessionAs(members.alex));
  server.budget = budget;
  const rendered = renderShell(server);
  await act(() => rendered.dispatcher.start());
  await screen.findByRole("button", { name: `Account: ${members.alex.name}` });
  return { server, ...rendered };
}

describe("the demo bar's budget", () => {
  it.each([
    ["ok", budgets.ok, "Live · $7.40 left"],
    ["session_exhausted", budgets.sessionExhausted, "Session budget used up"],
    ["daily_exhausted", budgets.dailyExhausted, "Today's demo budget is used up"],
    ["demo_exhausted", budgets.demoExhausted, "The demo's live-model budget is used up"],
    ["demo_exhausted by a provider block", budgets.providerBlocked, "The demo's live-model budget is used up"],
    ["paused", budgets.paused, "Live models are paused"],
  ])("reads %s", async (_state, budget, line) => {
    await renderWithBudget(budget);

    const item = await within(demoBar()).findByRole("button", { name: line });
    expect(item.getAttribute("title")).toBe("This session's live-model budget");
    // Not a live region: the bar's status is the stream's.
    expect(within(demoBar()).queryByRole("status")).toBeNull();
  });

  it("isn't shown while hosted caps are off", async () => {
    const { server } = await renderWithBudget(budgets.off);

    await waitFor(() => {
      expect(server.count("GET /api/budget")).toBe(1);
    });
    expect(within(demoBar()).queryByTitle("This session's live-model budget")).toBeNull();
  });

  it("isn't read before a session starts", async () => {
    const server = new FakeServer(null);
    const { dispatcher } = renderShell(server);
    await act(() => dispatcher.start());

    expect(screen.getByText("Welcome to Harmony")).toBeTruthy();
    expect(server.count("GET /api/budget")).toBe(0);
  });

  it("polls every 30 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { server } = await renderWithBudget(budgets.ok);
    await within(demoBar()).findByRole("button", { name: "Live · $7.40 left" });

    server.budget = budgets.paused;
    await act(() => vi.advanceTimersByTimeAsync(30_000));

    await within(demoBar()).findByRole("button", { name: "Live models are paused" });
    expect(server.count("GET /api/budget")).toBe(2);
  });

  it("refetches when a call is refused for budget", async () => {
    const { server, sources } = await renderWithBudget(budgets.ok);
    await within(demoBar()).findByRole("button", { name: "Live · $7.40 left" });
    act(() => {
      sources.latest.accept();
    });

    server.budget = budgets.sessionExhausted;
    act(() => {
      sources.latest.send(demoNotice(8, "needs_human"));
    });
    expect(server.count("GET /api/budget")).toBe(1);
    act(() => {
      sources.latest.send(demoNotice(9, "budget"));
    });

    await within(demoBar()).findByRole("button", { name: "Session budget used up" });
    expect(server.count("GET /api/budget")).toBe(2);
  });
});
