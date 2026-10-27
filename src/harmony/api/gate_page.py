"""The passcode page (LIVE_DEMO_PLAN 3.2): self-contained HTML, CSS and JS, served by
the server rather than the SPA, whose bundle names the cast.

`PAGE` is a constant: nothing from the request is ever written into it. Its
`Content-Security-Policy` allows only its own inline script and style, by hash.

Owner: L1.
"""

from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
from typing import Final

from fastapi.responses import HTMLResponse

# On every response the gate makes itself.
NO_STORE: Final = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}

# The script works out `next` itself and checks it with the URL parser, which strips
# tab and newline, so a prefix check alone would let `/%09/evil.example` through to
# `//evil.example`.
_SCRIPT: Final = """
(() => {
  const form = document.getElementById("gate");
  const input = document.getElementById("passcode");
  const button = document.getElementById("open");
  const error = document.getElementById("error");
  const sameOrigin = (value) => {
    try {
      const url = new URL(value, location.origin);
      return url.origin === location.origin ? url.pathname + url.search + url.hash : "/";
    } catch {
      return "/";
    }
  };
  const next =
    location.pathname === "/api/gate"
      ? new URLSearchParams(location.search).get("next") || "/"
      : location.pathname + location.search + location.hash;
  const show = (text) => {
    error.textContent = text;
    error.hidden = false;
  };
  const unreachable = "Harmony can't be reached. Try again in a moment.";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;
    try {
      const response = await fetch("/api/gate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: input.value, next: sameOrigin(next) }),
        credentials: "same-origin",
      });
      if (response.ok) {
        const body = await response.json();
        const target = new URL(sameOrigin(body.next), location.origin);
        // To this same URL, replace() only moves to the fragment: the gate would stay.
        if (target.pathname + target.search === location.pathname + location.search) {
          location.reload();
        } else {
          location.replace(target.href);
        }
        return;
      }
      if (response.status === 429) {
        const seconds = Number(response.headers.get("Retry-After")) || 60;
        const minutes = Math.max(1, Math.ceil(seconds / 60));
        show(`Too many attempts. Try again in ${minutes} minute${minutes === 1 ? "" : "s"}.`);
        return;
      }
      // Only the gate's own refusal says the passcode is wrong: a proxy's 502 while the
      // app restarts, or the app's 500, says nothing about it.
      const problem = await response.json().catch(() => null);
      if (response.status === 401 && problem?.code === "passcode_invalid") {
        show("That passcode isn't right.");
      } else if (response.status >= 500 || problem === null) {
        show(unreachable);
      } else {
        show("Harmony couldn't check that passcode. Try again.");
      }
    } catch {
      show(unreachable);
    } finally {
      button.disabled = false;
    }
  });
})();
"""

# The app's light tokens (web/src/theme/tokens.css); no fonts from the bundle.
_STYLE: Final = """
:root {
  color-scheme: light;
  --bg: #faf9f5;
  --surface: #ffffff;
  --border-strong: #d9d6ca;
  --text: #141413;
  --text-muted: #73726c;
  --accent: #d97757;
  --primary-btn: #141413;
  --primary-btn-hover: #2f2f2c;
  --danger: #b93a32;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 0 16px;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { width: min(22rem, 100%); }
h1 { margin: 0 0 1.5rem; font-size: 1.5rem; font-weight: 600; }
label { display: block; margin-bottom: 0.375rem; color: var(--text-muted); font-size: 0.875rem; }
input {
  width: 100%;
  padding: 0.625rem 0.75rem;
  border: 1px solid var(--border-strong);
  border-radius: 0.5rem;
  background: var(--surface);
  color: inherit;
  font: inherit;
}
input:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button {
  width: 100%;
  margin-top: 1rem;
  padding: 0.625rem;
  border: 0;
  border-radius: 0.5rem;
  background: var(--primary-btn);
  color: #ffffff;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
button:hover { background: var(--primary-btn-hover); }
button:disabled { opacity: 0.6; cursor: default; }
#error { margin: 0.75rem 0 0; color: var(--danger); font-size: 0.875rem; }
"""

PAGE: Final = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Harmony</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<h1>Harmony</h1>
<form id="gate" method="post" action="/api/gate">
<label for="passcode">Passcode</label>
<input id="passcode" name="passcode" type="password" autocomplete="current-password"
  required autofocus>
<button id="open" type="submit">Open</button>
<p id="error" role="alert" hidden></p>
</form>
</main>
<script>{_SCRIPT}</script>
</body>
</html>
"""


def _csp_hash(source: str) -> str:
    return "'sha256-" + b64encode(sha256(source.encode()).digest()).decode() + "'"


PAGE_HEADERS: Final = {
    "Content-Security-Policy": (
        f"default-src 'none'; script-src {_csp_hash(_SCRIPT)}; "
        f"style-src {_csp_hash(_STYLE)}; connect-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    **NO_STORE,
}


def page(status_code: int) -> HTMLResponse:
    return HTMLResponse(PAGE, status_code=status_code, headers=PAGE_HEADERS)


__all__ = ["NO_STORE", "PAGE", "PAGE_HEADERS", "page"]
