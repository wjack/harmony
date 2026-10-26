// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { TooltipProvider } from "../shell/ui";
import { WorldSwitch } from "./NetworkHeader";

afterEach(cleanup);

it("shows the big run as coming later, not as a world to open", () => {
  const onChange = vi.fn();
  render(
    <TooltipProvider>
      <WorldSwitch world="desk" onChange={onChange} />
    </TooltipProvider>,
  );
  const bigRun = screen.getByRole("radio", { name: "Big run" });
  expect(bigRun.getAttribute("aria-disabled")).toBe("true");
  expect(bigRun.getAttribute("aria-description")).toMatch(/^Coming later/);
  fireEvent.click(bigRun);
  expect(onChange).not.toHaveBeenCalled();

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
