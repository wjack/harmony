import { expect, test, type Page, type Route } from "@playwright/test";

import { passcode } from "./unlock";

/**
 * The master passcode gate from a locked visitor's side (LIVE_DEMO_PLAN 3.2): the
 * passcode page is all they get, a wrong passcode says so in place, the right one opens
 * the app where they were headed, and no `next` can send them to another site.
 *
 * In integration this runs against the real gate (compose-smoke). With
 * `E2E_GATE_STUB=1` it runs against the Vite dev server instead, `page.route` standing
 * in for the gate: its 401s, a passcode page that behaves as 3.2 specifies, and unlock.
 */
const STUB = process.env.E2E_GATE_STUB === "1";

/** L1's hostile `next` values, as they'd sit in the passcode page's query string. */
const HOSTILE_NEXT = ["//evil.com", "/\\evil.com", "https://evil.com", "/%09/evil.com", "/%0a/evil.com", "/%5C%5Cevil.com"];

const passcodeField = (page: Page) => page.locator('input[type="password"]');

/** Where the page tried to take the visitor off-site; such requests never leave. */
let offsite: string[] = [];

test.beforeEach(async ({ page, baseURL }) => {
  offsite = [];
  await page.route(
    (url) => url.hostname === "evil.com" || url.hostname.endsWith(".evil.com"),
    async (route) => {
      offsite.push(route.request().url());
      await route.abort();
    },
  );
  if (STUB) await stubGate(page, new URL(baseURL ?? "").origin);
});

test.afterEach(() => {
  expect(offsite).toEqual([]);
});

test("a locked visitor gets only the passcode page, and the API refuses them", async ({ page }) => {
  const scripts: string[] = [];
  const reached: string[] = [];
  page.on("request", (request) => {
    if (request.resourceType() === "script") scripts.push(request.url());
  });
  page.on("response", (response) => {
    const { pathname } = new URL(response.url());
    if ((pathname === "/api/session" || pathname === "/api/events") && response.ok()) reached.push(pathname);
  });

  const response = await page.goto("/requests");
  expect(response?.status()).toBe(401);
  await expect(page.getByRole("heading", { name: "Harmony", exact: true })).toBeVisible();
  await expect(passcodeField(page)).toBeVisible();
  await expect(page.getByRole("button", { name: "Open", exact: true })).toBeVisible();
  // The app's bundle never loads: it names the cast and the story.
  expect(scripts).toEqual([]);
  await expect(page.getByText("Welcome to Harmony")).toHaveCount(0);

  const statuses = await page.evaluate(() =>
    Promise.all(["/api/session", "/api/events"].map(async (path) => (await fetch(path)).status)),
  );
  expect(statuses).toEqual([401, 401]);
  expect(reached).toEqual([]);
});

test("a wrong passcode says so in place", async ({ page }) => {
  await page.goto("/");
  await passcodeField(page).fill(`${passcode()}-not`);
  await page.getByRole("button", { name: "Open", exact: true }).click();

  await expect(page.getByText("That passcode isn't right.")).toBeVisible();
  await expect(passcodeField(page)).toBeVisible();
  await expect(page.getByText("Welcome to Harmony")).toHaveCount(0);
});

test("a failure that isn't the gate's refusal never says the passcode is wrong", async ({ page }) => {
  // The stand-in gate serves its own copy of the page; only the real one is under test.
  test.skip(STUB, "needs the real passcode page");
  const failures = [
    { status: 502, contentType: "text/html", body: "<html><body>Bad gateway</body></html>" },
    { status: 503, contentType: "text/html", body: "<html><body>Service unavailable</body></html>" },
    { status: 500, contentType: "application/problem+json", body: JSON.stringify({ detail: "boom", code: "internal" }) },
    { status: 504, contentType: "text/plain", body: "" },
  ];
  await page.goto("/");
  for (const failure of failures) {
    await page.route("/api/gate", (route) =>
      route.request().method() === "POST" ? route.fulfill(failure) : route.fallback(),
    );
    await passcodeField(page).fill(passcode());
    await page.getByRole("button", { name: "Open", exact: true }).click();
    await expect(page.getByText("Harmony can't be reached. Try again in a moment.")).toBeVisible();
    await expect(page.getByText("That passcode isn't right.")).toHaveCount(0);
    await page.unroute("/api/gate");
  }
  // With the gate answering again, the same passcode opens the app.
  await page.getByRole("button", { name: "Open", exact: true }).click();
  await expect(page.getByText("Welcome to Harmony")).toBeVisible();
});

test("the right passcode opens the app at the landing", async ({ page, baseURL }) => {
  await page.goto("/");
  await passcodeField(page).fill(passcode());
  await page.getByRole("button", { name: "Open", exact: true }).click();

  await expect(page.getByText("Welcome to Harmony")).toBeVisible();
  expect(page.url()).toBe(new URL("/", baseURL).href);
});

test("unlocking a deep link goes back to it", async ({ page, baseURL }) => {
  await page.goto("/network");
  await passcodeField(page).fill(passcode());
  await page.getByRole("button", { name: "Open", exact: true }).click();

  // No session yet, so the app shows its landing, at the page they asked for.
  await expect(page.getByText("Welcome to Harmony")).toBeVisible();
  expect(page.url()).toBe(new URL("/network", baseURL).href);
});

for (const next of HOSTILE_NEXT) {
  test(`next=${next} lands on /`, async ({ page, baseURL }) => {
    await page.goto(`/api/gate?next=${next}`);
    await passcodeField(page).fill(passcode());
    await page.getByRole("button", { name: "Open", exact: true }).click();

    await expect(page.getByText("Welcome to Harmony")).toBeVisible();
    expect(page.url()).toBe(new URL("/", baseURL).href);
  });
}

// --- the stand-in gate --------------------------------------------------------------

const PASSCODE_REQUIRED = { detail: "passcode required", code: "passcode_required" };

/**
 * A passcode page that behaves as 3.2 specifies: a constant, which works out `next`
 * itself and checks it with the URL parser, then navigates to the server's answer after
 * the same check.
 */
const PASSCODE_PAGE = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Harmony</title></head>
<body>
<main>
<h1>Harmony</h1>
<form id="gate">
<input id="passcode" type="password" aria-label="Passcode" autocomplete="current-password" required autofocus>
<button type="submit">Open</button>
<p id="error" role="alert" hidden></p>
</form>
</main>
<script>
const sameOrigin = (next) => {
  try {
    const url = new URL(next, location.origin);
    return url.origin === location.origin ? url.pathname + url.search + url.hash : "/";
  } catch {
    return "/";
  }
};
const next = sameOrigin(
  location.pathname === "/api/gate"
    ? new URLSearchParams(location.search).get("next") ?? "/"
    : location.pathname + location.search + location.hash,
);
document.getElementById("gate").addEventListener("submit", async (event) => {
  event.preventDefault();
  const passcode = document.getElementById("passcode").value;
  const response = await fetch("/api/gate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passcode, next }),
  });
  if (response.ok) {
    location.replace(sameOrigin((await response.json()).next));
    return;
  }
  const error = document.getElementById("error");
  error.textContent = "That passcode isn't right.";
  error.hidden = false;
});
</script>
</body>
</html>`;

/** The server's own check of `next`: exactly one leading slash, no backslash or control character. */
function serverNext(next: string): string {
  let decoded: string;
  try {
    decoded = decodeURIComponent(next);
  } catch {
    return "/";
  }
  // eslint-disable-next-line no-control-regex -- control characters are what it refuses
  const unsafe = (path: string) => !/^\/(?![/\\])/.test(path) || /[\\\u0000-\u001f\u007f]/.test(path);
  return unsafe(next) || unsafe(decoded) ? "/" : next;
}

/**
 * Until the visitor unlocks, every path on `origin` gets the gate's answer: `/api/*` a
 * 401 `passcode_required`, anything else the passcode page with status 401. After,
 * the app comes from the dev server, and its API answers as for a visitor with no
 * session yet.
 */
async function stubGate(page: Page, origin: string): Promise<void> {
  let unlocked = false;
  await page.route(
    (url) => url.origin === origin,
    async (route: Route) => {
      const request = route.request();
      const { pathname } = new URL(request.url());
      if (pathname === "/api/gate") {
        if (request.method() === "GET") {
          await route.fulfill({ status: 200, contentType: "text/html", body: PASSCODE_PAGE });
          return;
        }
        const body = request.postDataJSON() as { passcode: string; next?: string };
        if (body.passcode.trim() !== passcode()) {
          await route.fulfill({ status: 401, json: { detail: "passcode invalid", code: "passcode_invalid" } });
          return;
        }
        unlocked = true;
        await route.fulfill({ status: 200, json: { next: serverNext(body.next ?? "/") } });
        return;
      }
      if (pathname.startsWith("/api/")) {
        await route.fulfill({
          status: 401,
          json: unlocked ? { detail: "No session.", code: "no_session" } : PASSCODE_REQUIRED,
        });
        return;
      }
      if (unlocked) {
        await route.fallback();
        return;
      }
      await route.fulfill({ status: 401, contentType: "text/html", body: PASSCODE_PAGE });
    },
  );
}
