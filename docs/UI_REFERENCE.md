# UI reference: replicating Claude

Harmony is a tool that lives inside Claude, so the web client is a close clone of the Claude web/desktop app. Harmony's own surfaces (Requests inbox, outreach approval cards, network view, demo controls) are styled as if Anthropic had shipped them. This file records what to copy, with the Mobbin screens it came from. The screens show 2026 Claude (Sonnet 4.6 era); swap in our model names.

## 1. Visual language

| Token | Value (approximate, from screenshots) | Notes |
|---|---|---|
| `--bg` | `#FAF9F5` | Main canvas, warm off-white |
| `--bg-sidebar` | `#F5F4EE` | Slightly darker than canvas, 1px right border `#E8E6DC` |
| `--bg-user-bubble` | `#F0EEE6` | User message pill |
| `--surface` | `#FFFFFF` | Composer, cards, menus, modals |
| `--border` | `#E8E6DC` | Hairlines; cards use 1px border + very soft shadow |
| `--text` | `#141413` | Body |
| `--text-muted` | `#73726C` | Timestamps, "Used Slack integration" lines, section labels |
| `--accent` | `#D97757` | The Claude spark, focus rings |
| `--accent-strong` | `#C6613F` | Send button (rounded square, white arrow) |
| `--primary-btn` | `#141413` bg / white text | "Save changes", "New chat", "Export"; 8px radius |
| `--toggle-on` | `#2C84DB` | Settings toggles are blue when on |
| Radii | 8px buttons/inputs, 12px menus/modals, 18–20px composer | |
| Display font | Serif (Claude uses Copernicus/Tiempos) → **Source Serif 4** or **Newsreader** (Google Fonts) | Greeting, page titles ("Settings", "Chats"), the "Claude" wordmark, **assistant message body** (default chat font is serif) |
| UI font | Sans (Claude uses Styrene) → **Inter** | Sidebar, buttons, labels, user messages |
| Mono | **JetBrains Mono** | Tool input/output |

Density: 13–14px UI text, 16–17px assistant text, generous line height (1.6). Hover states are a light tint (`#EFEDE5`); the selected sidebar row has the same tint with rounded 8px corners.

## 2. App shell

Ref: [home, Pro](https://mobbin.com/screens/d72b17b4-df91-4813-8e5d-a04d20041570), [home, Welcome](https://mobbin.com/screens/dc0671c0-a055-45bd-92dc-42cadf7c1934), [dark](https://mobbin.com/screens/66e2902a-c536-4bff-8d86-689b26dfd2ee), [collapsed sidebar](https://mobbin.com/screens/508e971b-a955-4b68-9d5f-63576b9e3166)

**Sidebar** (~288px, collapsible to a 48px icon rail):
- Top: serif "Claude" wordmark, collapse icon on the right.
- Group 1: **New chat** (plus in a circle), **Search**, **Customize**.
- Group 2: **Chats**, **Projects**, **Artifacts**, **Code**. Harmony adds **Requests** here, with a count badge, and **Network** (see §7, §8).
- "Starred" and "Recents" section labels (muted, 12px) with conversation titles truncated with an ellipsis; a hover shows a `…` menu.
- **Harmony** lists requests the way Claude lists recent chats: in the expanded sidebar, above Recents and in the same scroll area, two more sections in the same label style:
  - **Needs you:** every item waiting on the viewer. Two lines per row: a warning dot and the title, then the counterparty in muted text; an unread dot on the right. A requester-side question opens its chat.
  - **Recent requests:** the viewer's other request threads, newest activity first, at most 10, then an "All requests" row that opens `/requests`. One line per row, the title; the counterparty is in the tooltip. Rows always open the request view.
  - The collapsed rail keeps only the Requests badge.
- Bottom: avatar circle (initials), name, plan line ("Pro plan" → for us, the org name, e.g. "Henderson Lab"), a download icon, and an up/down chevron that opens the account menu.

**Top bar** (inside a chat): the chat title with a chevron dropdown on the left, **Share** button on the right ([chat](https://mobbin.com/screens/c7ef4781-6ed2-4ac8-a41c-fe50a1271628)). On the home screen, only a small icon at top right.

## 3. Home / new chat

Ref: [home](https://mobbin.com/screens/d72b17b4-df91-4813-8e5d-a04d20041570), [plus menu](https://mobbin.com/screens/16cc5d91-2f00-4a92-9777-6dd57a63a6a4)

- Centered greeting: orange spark + serif "Good afternoon, Alex" (~40px).
- Composer card: white, 20px radius, soft shadow, ~680px wide. Placeholder "How can I help you today?". Bottom row: `+` button (left), model label (right), send button (orange rounded square, disabled = pale orange) or a voice waveform icon when empty.
  - **Harmony:** the model label names the model internal Claude actually runs on for the acting member's org: "{model} · Adaptive ⌄" when its thinking is adaptive ("Opus 5.5 · Adaptive", "Haiku 5.5 · Adaptive"), the label alone otherwise ("Scripted" in scripted mode). The model follows the org, so clicking it opens the "not available" popup.
  - **Harmony:** while the chat sends Harmony Network Requests without review, an amber chip, "Sends without review" with a warning-triangle icon, sits beside the model label. Clicking it opens the + menu's Harmony submenu.
- Suggestion chips under the composer: outlined pills with an icon (Write, Learn, Code, Life stuff, Claude's choice).
  - **Harmony:** the walkthrough's suggested initial message appears as a chip (e.g. "✦ Find a facility for 40 cryo-EM grids") that fills the composer on click, plus a subtle "Suggested" label. No forced steps.
- `+` menu: Add files or photos, Take a screenshot, Add to project ›, Connectors ›, Research, Web search ✓, Use style ›. In Harmony every item except **Connectors › Harmony** opens the "not available in this prototype" popup.
  - **Connectors › Harmony** is a submenu with three radio items: "Organization default · {Review before sending | Send without review}", "Review before sending" and "Send without review". It sets this chat's mode; a new chat carries the choice into its first message.

## 4. Conversation

Ref: [tool use + question card](https://mobbin.com/screens/9f5be565-9c27-40d3-9c3e-aadf582fac46), [response](https://mobbin.com/screens/c7ef4781-6ed2-4ac8-a41c-fe50a1271628)

- **User message:** right-aligned pill, `--bg-user-bubble`, sans, max ~70% width.
- **Assistant message:** no bubble, serif body, markdown (bold, bullet lists, headings). Under it: icon row (copy, thumbs up, thumbs down, retry), visible on hover.
- **Tool activity line:** muted one-liner above the answer, e.g. "Used Slack integration, loaded tools ›" or "Ascertained connection status and sought clarification ›". Click to expand into the tool calls.
  - **Harmony:** "Searched the Harmony network · 14 organizations ›", "Checked request status ›".
- **Tool result card:** bordered box with an icon, title and right-aligned meta. Example: "Connector search · 10 connectors", then a body line listing the connectors.
  - **Harmony:** a **Harmony Network Request card** that shows the recipients, each one's status (Messaged / Replied / Needs human at Northlake / Closed) with the latest line or, once closed, our side's summary, and an "Open in Requests" link. It updates live from SSE.
- **Streaming indicator:** the orange spark alone on a line (animated).
- **Question card above the composer** (Claude's ask-user pattern): a white card attached to the top of the composer. Its layout:
  - question title, a "1 of 2 ‹ ›" pager and a close ×;
  - numbered option rows (rounded, hover tint, → on the focused row);
  - a final "Something else" free-text row with a **Skip** button;
  - a keyboard hint footer ("↑↓ to navigate · Enter to select · Esc to skip").
  - **Harmony uses this exact pattern for the `send_network_request` approval:**
    - the card's heading reads "Harmony Network Request" (the same heading on the status card and the auto card, so the region's name never changes), and the request's own title is the body's first line;
    - title "Send this request to 2 organizations?";
    - the brief as free text, and the recipients with their screening fit and reason ("Screened 34 orgs: 3 strong, 4 possible");
    - options "Send", "Send to Northlake only", "Edit brief…";
    - typing in the "Something else" row (or the composer) counts as rejecting with feedback, as in Claude Code;
    - long text (the brief, the recipient names, each recipient's ask) is clamped with "View more" / "View less", never an inner scroll.
  - **The auto card** (a chat set to send without review): the same "Harmony Network Request" heading, no buttons, and one line by status: "Sending…", "Sent without review", or "Not sent: {reason}". The request status card follows as usual.
- **Inline question card** (Harmony): when our representative consults and the question is routed to this member, a card appears in the origin chat with the question, the organization it came from, and an answer box; once resolved, it shows the answer. For Alex, that's Ridgeline asking which seal material the holder needs. Answering releases the answer to that one thread.
- There are no compare-offers or accept cards. Prices and terms arrive as messages, and internal Claude summarizes them in prose.
- **Notify banner:** "Want to be notified when Claude responds? [Notify] ×" in a tinted strip above the composer. Reuse it for "Responses from the network will appear here as they arrive."
- **Footer disclaimer:** "Claude is AI and can make mistakes. Please double-check responses."

## 5. Account menu (switch accounts)

Ref: [menu](https://mobbin.com/screens/845e141c-581c-4cde-b348-b65d9e3f9abc), [menu + language](https://mobbin.com/screens/da3d700f-0862-4c7d-94b0-88526bdcbda6)

- A popover anchored above the sidebar footer, 12px radius, white. It contains:
  - the email in muted text;
  - Settings (⇧⌘, shortcut on the right), Language ›, Get help;
  - a divider, then Upgrade plan, Get apps and extensions, Gift Claude, Learn more ›;
  - a divider, then Log out.
- **Harmony:**
  - After the email, insert a **"Switch account"** group listing the demo personas: avatar, name, org, role, and a ✓ on the active one.
  - Add **Demo controls** (opens the modal) and **Reset demo**.
  - "Log out" becomes **Lock Harmony**: it clears the passcode cookie and shows the passcode page.
  - Everything else opens the "not available" popup.

## 6. Settings and Customize

Ref: [General](https://mobbin.com/screens/8fa0bf04-4ef2-4478-96f7-0db85c19abfd), [Account](https://mobbin.com/screens/d25e68ff-d225-4841-9377-d2b81bad9927), [Connectors](https://mobbin.com/screens/e6ade293-ed67-4901-8d3c-4196c31b842e), [Customize](https://mobbin.com/screens/b77733cb-f80c-4f1f-b697-2283f0517227), [Connector detail with tool permissions](https://mobbin.com/screens/2eb6c9a1-64f2-46a8-9cf8-ba9b1897ba86)

- **Settings:**
  - serif "Settings" title;
  - left sub-nav (General, Account, Privacy, Billing, Usage, Capabilities, Connectors, Claude Code, Claude in Chrome Beta) with a tinted selected row;
  - the content column uses 15px bold section headings, two-column inputs with rounded borders, a select, a textarea, blue toggles, a hairline divider between sections, and Cancel plus a black "Save changes".
- **Customize:**
  - a second-level sidebar (← Customize, Skills, Connectors), a list column, and a detail pane;
  - the connector detail has a "Tool permissions" section: groups ("Interactive tools 1", "Read-only tools 11") with an "Always allow ⌄" dropdown, and per-tool rows with a 3-way icon segmented control (allow ✓ / ask ✋ / block ⊘).
- **Harmony:**
  - Add a **Harmony** entry under Customize (org admins edit; other members view) with these sections, in the Settings content-column style:
    - **Public profile**: one **Summary** textarea and the directory visibility select.
    - **Instructions**: one textarea.
    - **Sharing**: the default sharing description (a textarea: what kinds of information Claude may pass on), then the **Trusted organizations** list. "Add organization" opens an org picker. Each row shows the org, a mode select ("Same as default", Ask, Auto, Skip; it defaults to "Same as default"), an "Also share with them" textarea, and remove.
    - **Autonomy**: the default mode as three options, each explained by when a person gets involved:
      - **Ask**: "A teammate approves every answer and message before it goes to the other organization."
      - **Auto** (recommended): "Claude handles the conversation. A teammate sees anything beyond what you share, and any decision that needs a person."
      - **Skip**: "Claude handles everything it can. A teammate sees only what Claude says needs a person. For partners you trust fully."

      Next to it, a line saying trusted organizations can have their own mode, linking to Sharing.

      Below it, a second section, **Sending Harmony Network Requests**, led by "For requests Claude sends to other organizations for your members. It doesn't change how your representative answers them." Two options:
      - **Review before sending** (recommended): "Claude shows the brief and the organizations it chose. Nothing goes out until you approve."
      - **Send without review**: "Claude sends requests as soon as it writes them, to the organizations it chose. Nobody checks what is asked or who is asked. Spend limits still apply."

      A warning callout shows while the second is chosen, and a note says "Members can change this for one chat from the composer's + menu." Non-admins see it read-only.
    - **Team & routing**: the default assignee, the routing prompt, and the members with their role and profile prose.
  - There are no Files and knowledge, Standing permissions, Offers and authority, Holding message or routing-learning sections, and no per-category permission controls. Every control for a removed concept is gone, not shown as unavailable.

## 7. Requests (Harmony surface)

Ref: [Linear inbox](https://mobbin.com/screens/8337813e-f0dd-4415-8a29-87c114b0442b) (the section grouping only)

Requests behave like chats: the sidebar lists them (§2), `/requests` is a full list page, and a request opens full width. There is no list column beside the open request and no always-on properties rail.

- **List page (`/requests`):** like the Chats page. A serif page title, search, an All / Inbound / Outbound filter, and five sections: **Needs you**, **With your team** (an item open for a teammate, e.g. after a reassign), **Claude is handling**, **Shared with you**, **Done**. Each row shows the title, then "counterparty · status · age" in muted text, and an unread dot.
- **Request view (`/requests/:threadId`):**
  - **Header:** the short title, "with {counterparty}", a status pill ("Needs your answer", "Reply needs your review", "Needs Raj", "Claude is handling", "Checking with the team", "Waiting on {counterparty}", "Closed", "Expired", "Shared by Raj", "Stopped") and a **Details** button. On a phone the pill is only its dot, so the title fits.
  - **Transcript:** chat-style, in a 768px column. Each message has an avatar, "Claude for Northlake · 8:38 AM" and a serif body. Both sides are agents, so neither gets a bubble, and both are left-aligned. A message over 12 lines is clamped with "View more".
  - **Our side's rows:** a consult is one line, "Our Claude asked the team · only Henderson Lab sees this", with a lock and the full question behind the chevron; a release is one line, with the detail behind the chevron; a note shows in full, its author's avatar and "Raj Patel · Internal note · 7:42 AM" over the body in a dashed box; while a side is writing, its avatar and "Claude for Ridgeline is writing…", or our side's streamed text in the message style. A close is a "Closed" row; a stopped thread shows a banner such as "Stopped: the demo's live budget ran out."
  - **Decision card,** inline after the last row, scrolling with it (never docked, no inner scroll):
    1. an eyebrow: "Claude needs your answer before replying to Henderson Lab", or "Claude wants to send this to Henderson Lab", naming the assignee when it isn't the viewer ("Claude needs Raj's answer before replying to Henderson Lab");
    2. a title: the one-line question, or "Send this reply to Henderson Lab?";
    3. **Why it came to you:** why a person is needed ("Reserve slots aren't in what you share"), then "Routed to you: you run instrument scheduling" with **Reassign**, or "Sent to you as Northlake's default assignee";
    4. **Draft reply:** serif text with a revision pager; Edit turns it into a textarea that grows to its content;
    5. **Looked at:** "public calendar · price list · Raj's note", one line that expands to the tool-activity lines in the chat style ("Searched Northlake's records · reserve XRD ›");
    6. "Claude's reasoning" (collapsed), and "Edit what you share with Henderson Lab".
  - **One composer** at the bottom, in the chat composer's style:
    - no open item: "Add an internal note… Use @ to bring in a teammate", with **Add note**;
    - an open item, empty: **Decline**, **Edit**, **Approve and send** (⌘↵);
    - an open item, with text: **Decline**, **Add note**, and **Redraft** for a question or **Send back to Claude** for a held message (text with an @mention offers only **Add note**);
    - **Decline** sets any typed note aside and switches the composer to "The reason Claude can give Henderson Lab (optional)…", with Cancel (which restores the note) and **Decline**, captioned "Claude may pass this reason on to Henderson Lab · Esc to cancel";
    - a caption: "Approving sends this reply to Henderson Lab only · ⌘↵ to approve" ("Edited · " in front while the draft is edited, and no "· ⌘↵ to approve" while text is typed), "Approving answers for Raj · sends this reply to Henderson Lab only" when the viewer isn't the assignee, or "Only Northlake sees notes".
  - **Details** (a right panel, closed by default and remembered per viewer; below 820px a sheet that always starts closed): Share, "What our representative saw", the properties (Status, Assignee, Counterparty, Expires) and the activity log ("Claude drafted a reply · 2m", "Helen released the answer · 1m").
  - "with {counterparty}" in the header hides when the transcript column is narrower than 512px (beside an open Details panel, or on a phone). A Stopped banner replaces the Closed or Expired one.

## 8. Network view (Harmony surface)

Ref: [Railway canvas](https://mobbin.com/screens/8a0b69ca-8b37-493f-bfbe-883b32bbe825), [Cofounder node + side panel](https://mobbin.com/screens/4da116d0-2cec-4986-8121-ad606a2d0d3f), [Clay flow with status pills](https://mobbin.com/screens/a6ac0216-33ab-4e7e-9e1f-3d83938c22f3)

A live lens over the current world, built with React Flow:

- **Canvas:** dotted background (Railway); zoom, fit and auto-layout controls at bottom left.
- **Org nodes:** white cards (Railway/Clay), each with:
  - a monogram avatar, the org name and a one-line descriptor;
  - a status pill once the org takes part: `Screened` (grey), `Messaged` (blue), `Replied` (green), `Needs human` (orange, pulsing dot) or `Closed` (muted ✓).
- **Suggested recipients:** orange ring plus a "Suggested" tag; the walkthrough points at them, nothing more.
- **The requester's node** is pinned at the left and the others fan out right.
- **Layout:** the graph fills the canvas at a readable zoom, and the side panel is narrow enough not to dominate.
- **Edges:** an edge appears when a thread opens and animates (moving dash) while a turn is in flight. The edge label carries the latest line, truncated ("Kalrez adds $85 to the holder").
- **Side panel** (Cofounder), opened by clicking a node:
  - the org's name, kind and screening fit;
  - threads with this org (status, latest line, who's waiting). A closed thread shows the closer's summary to the closer's own org and "Closed" to everyone else;
  - "Act as Helen" to jump into that org's inbox when it needs a human.
- **Header strip:**
  - the request picker (which Harmony Network Request to show);
  - counts ("3 orgs · 2 threads · 1 active · 1 closed");
  - a world badge.

## 9. Modals, popups, toasts

Ref: [Export data modal](https://mobbin.com/screens/bbabc6b5-8534-45d8-bdca-a19c9aa6863c), [toast](https://mobbin.com/screens/77a7bdab-b92a-46d4-8abf-5d579e59d699)

- **Modal:**
  - white, 12px radius, ~480px wide, over a dim scrim;
  - a semibold title with × at top right and a muted description;
  - segmented pill choices (black = selected);
  - a bordered list box;
  - footer with a white "Cancel" and a black primary button.
- **"Not available in this prototype" popup:** the same modal shell, with the spark icon, "This isn't part of the Harmony prototype", one line on what *is* in scope, and an "OK" button.
- **Demo controls modal:** the same shell, with these sections:
  - Account (persona switcher);
  - World (reset; replaying the recorded big run arrives with the network world in P3);
  - Walkthrough (show suggested message, highlight suggested recipients);
  - Model and spend, read-only: which model each role runs on, and the session's live budget. Nothing here switches models.
- **Passcode page** (before the app loads; served by the server, so it isn't a Claude screen): the canvas color, a serif "Harmony" heading, one password field, an "Open" button, and inline errors: "That passcode isn't right." / "Too many attempts. Try again in N minutes."
- **Toast:** a small white pill at top right with an ⓘ icon, text and ×. Use it for "Northlake replied", "Switched to Dr. Helen Ward" and "Demo reset".

## 10. Out of scope (popup on click)

Projects, Artifacts, Code, Search, Starred, Share, file upload, screenshots, Research, Web search, styles, voice, Language, Get help, Upgrade, Gift, Learn more, Billing, Privacy, Usage, Capabilities, Claude Code, Claude in Chrome, Skills, non-Harmony connectors, dark mode toggle (dark mode itself may come for free from tokens).
