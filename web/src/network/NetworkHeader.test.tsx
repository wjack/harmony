// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { TooltipProvider } from "../shell/ui";
import { closeUnavailable, unavailableStore } from "../shell/unavailable";
import { WorldSwitch } from "./NetworkHeader";

afterEach(() => {
  cleanup();
  closeUnavailable();
});

it("shows the big run as coming later: it opens the not-available dialog, not a world", () => {
  const onChange = vi.fn();
  render(
    <TooltipProvider>
      <WorldSwitch world="desk" onChange={onChange} />
    </TooltipProvider>,
  );
  const bigRun = screen.getByRole("radio", { name: "Big run" });
  expect(bigRun.getAttribute("aria-checked")).toBe("false");
  expect(bigRun.getAttribute("aria-description")).toMatch(/^Coming later/);
  fireEvent.click(bigRun);
  expect(onChange).not.toHaveBeenCalled();
  expect(unavailableStore.getState().feature).toBe("The big run");

  // Whoever is already there can still go back to the desk.
  cleanup();
  render(
    <TooltipProvider>
      <WorldSwitch world="network" onChange={onChange} />
    </TooltipProvider>,
  );
  fireEvent.click(screen.getByRole("radio", { name: "Your desk" }));
  expect(onChange).toHaveBeenCalledWith("desk");
});
