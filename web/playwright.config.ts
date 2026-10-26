import { defineConfig } from "@playwright/test";

// Drives an app that is already up on ScriptedModel, named by E2E_BASE_URL:
// scripts/e2e_ui.sh brings up the containerized one, or takes one you run. There is no
// default, so the story never runs against an app nobody chose.
const baseURL = process.env.E2E_BASE_URL;
if (baseURL === undefined || baseURL === "") {
  throw new Error("E2E_BASE_URL is not set: run the story with `make e2e-ui` (scripts/e2e_ui.sh)");
}

export default defineConfig({
  testDir: "e2e",
  outputDir: "test-results",
  // One story, one world: nothing to parallelize, and a retry would hide a flaky turn.
  workers: 1,
  retries: 0,
  forbidOnly: process.env.CI !== undefined,
  reporter: [["list"]],
  use: {
    baseURL,
    viewport: { width: 1440, height: 900 },
    // Clicks and fills wait this long for their element; model turns are waited on
    // with explicit, longer expectations in the test.
    actionTimeout: 20_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
