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

**Harmony's type scale** (round 2, decision 16d; `web/src/theme/tokens.css`): `text-ui-xs` 12px for meta and timestamps, `text-ui-sm` 13px for secondary lines and hints, `text-ui` 14px for UI text and buttons, `text-ui-title` 15px for card titles, `text-ui-md` 16px, and `text-prose` 16px for serif messages and drafts. The rebuilt surfaces (the request view, the cards, the chat's outreach card) use it; the repo-wide sweep comes later.

**Harmony's colour rule** on the request view and the cards: black and white is what can reach the other org; amber is your team (a comment, its box while typing); blue is your own Claude (an Edit, "Tell Claude", what Claude rewrote). An edited field's ring is neutral, never blue.

## 2. App shell

Ref: [home, Pro](https://mobbin.com/screens/d72b17b4-df91-4813-8e5d-a04d20041570), [home, Welcome](https://mobbin.com/screens/dc0671c0-a055-45bd-92dc-42cadf7c1934), [dark](https://mobbin.com/screens/66e2902a-c536-4bff-8d86-689b26dfd2ee), [collapsed sidebar](https://mobbin.com/screens/508e971b-a955-4b68-9d5f-63576b9e3166)

**Sidebar** (~288px, collapsible to a 48px icon rail):
- Top: serif "Claude" wordmark, collapse icon on the right.
- Group 1: **New chat** (plus in a circle), **Search**, **Customize**.
- Group 2: **Chats**, **Projects**, **Artifacts**, **Code**. Harmony adds **Requests** here, with a count badge, and **Network** (see §7, §8).
- "Starred" and "Recents" section labels (muted, 12px) with conversation titles truncated with an ellipsis; a hover shows a `…` menu.
- **Harmony** lists requests the way Claude lists recent chats: in the expanded sidebar, above Recents and in the same scroll area, two more sections in the same label style:
  - **Needs you:** every item waiting on the viewer. Two lines per row: the title, then the counterparty in muted text. The section already says it needs you, so the row's one dot, on the right, means unread. A requester-side question opens its chat.
  - **Recent requests:** the viewer's other request threads, newest activity first, at most 10, then an "All requests" row that opens `/requests`. One line per row, the title; the counterparty is in the tooltip. Rows always open the request view.
  - The collapsed rail keeps only the Requests badge.
- **Width:** Harmony is desktop only, down to about 900px; there are no phone layouts. Below 900px the sidebar is the rail whatever the stored choice, and opening it lays it over the page until a link, the scrim or Escape closes it.
- Bottom: avatar circle (initials), name, plan line ("Pro plan" → for us, the org name, e.g. "Henderson Frame Lab"), a download icon, and an up/down chevron that opens the account menu.

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
  - **Harmony:** one step reads as its label and outcome after "·" ("Searched the Harmony network · 2 possible fits of 14 ›"); several name what was searched ("Searched the network for 2 needs and your records ›"), with a repeated step named once with its count. Expanded, each step is its label and outcome, with what it searched for (the need, or the query) on a second, smaller line.
- **Tool result card:** bordered box with an icon, title and right-aligned meta. Example: "Connector search · 10 connectors", then a body line listing the connectors.
  - **Harmony:** a **Harmony Network Request card** that shows the recipients, each one's status (Messaged / Replied / Needs human at Northlake / Closed) with the latest line or, once closed, our side's summary, and an "Open in Requests" link. It updates live from SSE.
- **Streaming indicator:** the orange spark alone on a line (animated).
- **Question card above the composer** (Claude's ask-user pattern): a white card attached to the top of the composer. Its layout:
  - question title, a "1 of 2 ‹ ›" pager and a close ×;
  - numbered option rows (rounded, hover tint, → on the focused row);
  - a final "Something else" free-text row with a **Skip** button;
  - a keyboard hint footer ("↑↓ to navigate · Enter to select · Esc to skip").
  - **Harmony doesn't use this pattern for the `send_network_request` approval** (round 2, decision 8; `docs/mockups/round2/outreach.html`). The approval is an **inline card** at the end of the chat: the request view's decision card in the chat's words.
    - **Head:** "Harmony Network Request" (the same heading on the status card and the auto card, so the region's name never changes), a "Waiting for you" pill and a fold chevron; then the request's own title, "To Northlake and Ridgeline", and how they were chosen ("Claude screened 14 organizations and picked 2").
    - **Brief** ("· Claude works from this in every conversation"), then **Who gets it** ("Untick to leave one out"): each recipient with a tick, its **Subject**, read-only ("Northlake sees this as written"), and its **Ask** ("· Claude works from this with Northlake only").
    - The brief and each ask are edited where they stand: a click puts the caret in the text, which grows with it and never clamps or scrolls inside. A changed part reads "edited by you" with **Undo my edits**; a part Claude rewrote reads "rewritten by Claude ‹ 2 of 2 ›".
    - **Foot:** **Decline** on the left, **Approve and send** on the right, and under them what approving does ("Starts a conversation with Northlake and Ridgeline · ⌘↵", with ", with your edits" when changed). ⌘↵ approves only with focus in the card. **Decline** opens a small step, "Don't send this request", with an optional reason captioned "Only Claude in this chat sees this. No organization hears about it."
    - While live turns are stopped, the budget strip sits on the card ("Nothing goes out until the budget is back").
    - While the card is out of view, a **Review** strip above the composer names it ("Send this request to 2 organizations?") and scrolls to it. The composer's placeholder reads "What should Claude do differently?": a message typed there turns the card down: the card folds to an "Earlier version" row, the message shows as the member's, and Claude reads it as feedback.
    - **Once decided** it folds to one line: "Sent" with "Sent to Northlake and Ridgeline · 9:12 AM" ("…Ridgeline, with your edits · 9:12 AM" when the member changed the wording) and "Show what was sent", or "Declined" with "Not sent" and the member's reason. A card a message turned down, or a newer draft replaced, is one quiet row: "Earlier version of this request · not sent · Show".
  - **The auto card** (a chat set to send without review): the same "Harmony Network Request" heading, no buttons, and a pill by status: "Sending…", "Sent without review", or "Not sent" with the reason. The request status card follows as usual.
- **Inline question card** (Harmony): when our representative consults and the question is routed to this member, a card appears in the origin chat with the question, the organization it came from, and an answer box; once resolved, it shows the answer. For Alex, that's Ridgeline asking which tube material the holder needs.
  - **Who gets the answer: only the organizations that asked.** The line under the box names them ("Answer goes to Ridgeline Fabrication"). When other conversations on the request ask the same thing first, they join the card ("Same question from 3", "Also asked by …", "How each asked"), each with a tick that starts on; the shown asker gets the words as written and each ticked one a version fitted to its question, checked for that organization. Untick one and it waits on its own. A conversation that asks after the answer gets its own card, never this answer.
  - **Or answer in the chat.** The member may just tell Claude instead of typing in the box: when their message answers a question waiting on them, even partly, Claude sends their words as its answer (`answer_question`, "Answered your representative's question" in the chat's activity), exactly as **Send answer** would, to the same askers, and the card shows it answered. Claude never asks a waiting question again in its own words; it may point to the card. It learns of the question from `get_request_status` (each conversation's `open_question`, with its ID) and, on the member's next message, from a note naming the organization and the question's ID (user, 2026-10-28: a reply in the chat used to sit unread while the conversation waited on the card).
  - Answered, the card lists "Where your answer went", one row per asker ("Sent as you wrote it", "Sent · fitted to Kestrel's question…", "Held for review" or "Not sent" with **Review**).
  - **Passing part of it on is asked in the chat.** Right after the answer, Claude in the chat looks once at the request's other open conversations. Only when a specific part would clearly help one ("they want delivery by Nov 6"), it asks in plain text, quoting the exact words and naming the organizations. Nothing goes further unless the member says yes there, and they may narrow it. On a yes it shares exactly those words with our own representative in each conversation named ("Shared with your representatives · Northlake Characterization Core and Calder Computational" in the chat's activity), never to a counterparty directly, and never naming one organization to another. That representative's "What our representative saw" lists it as "Shared by your side for this conversation".
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
  - the content column uses 15px bold section headings (Customize keeps them; the round-2 type scale doesn't apply there yet), two-column inputs with rounded borders, a select, a textarea, blue toggles, a hairline divider between sections, and Cancel plus a black "Save changes".
- **Customize:**
  - a second-level sidebar (← Customize, Skills, Connectors), a list column, and a detail pane. **Harmony** shows the second-level sidebar only from 1280px wide (round 2, decision 16c); below that the list column carries the way back ("←" before "Harmony");
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
- **Request view (`/requests/:threadId`)** (round 2: `docs/mockups/round2/composer.html`, `docs/design/EDIT.md` §8.3, `docs/design/REOPEN.md` §8):
  - **Header:** the short title, "with {counterparty}", a status pill and a **Details** button. The pill is one of "Needs your answer", "Reply needs your review", "Waiting on Raj" (an item for a teammate), "Claude is handling", "Checking with the team", "Waiting on {counterparty}", "Shared by Raj" and "Closed". However the conversation ended it reads "Closed"; there is no "Expired" or "Stopped". The pill keeps its width: "with {counterparty}" gives way first, then the title truncates (decision 16b).
  - **Transcript:** chat-style, in a 768px column. Each message has an avatar, "Claude for Northlake · 8:38 AM" and a serif body, in full, with no clamp. Both sides are agents, so neither gets a bubble, and both are left-aligned. A message a person approved or wrote says so ("Approved by Helen"). A close is a centred divider, "Claude for Northlake closed the conversation · 8:40 AM".
  - **Our side's rows** (only our org sees them; each says "only Northlake sees this"):
    - a **comment**, in amber: "Raj Patel · comment · 7:42 AM" over the body, with its @mentions highlighted. No Claude reads it;
    - an **Edit**, in blue: "Raj told Claude what to do differently", the instruction quoted, then how it went (Claude rewriting, the new draft, "See guidance", or why it failed with "Try again");
    - a **decision**: "Helen approved the answer", "Helen declined Claude's message" with the reason quoted and "Claude may pass this reason on to Henderson Frame Lab.", or "Claude answered without a teammate";
    - a **Closed row** per run that ended: "Closed · Oct 29 · {why}" ("both sides went quiet", "Northlake closed it", "Claude closed it", "nothing was sent", or why it was stopped), with our side's summary of that run under it ("Claude for Northlake's summary. Only Northlake sees it."). The latest carries **Reopen**, or a line saying why it can't be reopened, or why Reopen waits ("Claude is still wrapping up this conversation.", or the budget line). Reopen opens a small blue prompt in its place, "What should Claude pick up?", captioned "Claude writes the next message, following your usual review settings. Only Northlake sees what you typed.", with Cancel and **Reopen**;
    - a **reopen row**: "Raj is reopening the conversation", then "reopened" or "tried to reopen" with Try again, quoting what was typed; the counterparty's reads "Henderson Frame Lab reopened the conversation";
    - the **last row** says whose move it is, steadily: "Waiting for Henderson Frame Lab to reply" from the moment our message lands until theirs does, whatever their Claude is doing (decision 2); "Waiting on Raj" while a teammate's item is open; "Checking Northlake's records…" while our answerer works; or our Claude's streamed text in the message style while it writes. The other side never shows as writing, and nor does ours once the conversation has ended (its last turn only writes our summary).
  - **Decision card,** inline after the last row, scrolling with it (never docked, no inner scroll; its foot sticks while the card is taller than the view):
    1. an eyebrow: "Claude needs your answer before replying to Henderson Frame Lab", or "Claude wants to send this to Henderson Frame Lab", naming the assignee when it isn't the viewer ("Claude needs Raj's answer before replying to Henderson Frame Lab", "… · Raj reviews"), with **Reassign** on the right;
    2. a title: the one-line question, or "Send this reply to Henderson Frame Lab?";
    3. a muted reason line when something more than Ask mode stopped it ("Reserve slots aren't in what you share");
    4. **Draft reply** (or "Claude's message"): the whole draft in serif, edited where it stands by clicking the text ("Click the text to change the wording"). Its label reads "rewritten by Claude ‹ 2 of 2 ›" after an Edit, and "edited by you" with **Undo my edits** after a change. While an unsent Edit waits in the box, the draft is marked in blue ("Claude rewrites this when you tell it"); while Claude rewrites it, "Claude is rewriting…", dimmed and locked;
    5. **Looked at:** one line ("public calendar · price list") that expands to one row per tool call;
    6. "Edit what you share with Henderson Frame Lab" (an admin) or "See what you share with Henderson Frame Lab";
    7. the foot: **Decline** on the left and **Approve and send** on the right, nothing else. Under them, what approving does ("Claude replies to Henderson Frame Lab with this answer · ⌘↵", "Sends this message to Henderson Frame Lab as shown · ⌘↵") and, on a held message, "Ask mode with Henderson Frame Lab · Change" (opens Customize › Autonomy). ⌘↵ approves only with focus in the card. Approve waits while Claude rewrites, and both wait while the budget strip shows on the card. **Decline** opens a small step in the foot's place with a required reason: "Decline to answer", captioned "Claude tells Henderson Frame Lab you can't answer, and may pass on your reason.", or "Decline this message", captioned "Claude won't send this message. It may write another, which comes back to you first. Henderson Frame Lab may see your reason."
  - **One box** at the bottom, in the chat composer's style, with a **Comment | Edit** switch in its bottom bar. It starts in Comment and goes back to it after each Edit, and Enter sends what's in it, never approving.
    - **Comment:** "Comment for your team… Use @ to bring someone in", amber while typing, with **Add comment** and the caption "Only Northlake sees comments. Claude doesn't read them." An @mention shares the thread with that teammate; the mention menu lists the org's members.
    - **Edit:** "What should Claude do differently?", blue, with **Tell Claude** and the caption "Claude rewrites the draft and follows this for the rest of the conversation. Only Northlake sees it." (or, with no draft waiting, "Claude follows this from its next message in this conversation."). Mentions are for comments. Edit is off on a closed conversation ("Reopen the conversation to tell Claude what to pick up"); an Edit typed but not sent when the conversation closes moves to the Reopen prompt, which holds it while Claude wraps up. Tell Claude is off while the budget stops live turns.
  - **Details** (a right panel, closed by default and remembered per viewer; below 820px of view a sheet over the transcript that always starts closed): Share, "What our representative saw" (its prompt, the notices, "Guidance from your side" by version, and the released answers), **Guidance Claude follows** (the current guidance, "Version 2 · from Raj's edit · 9:14 AM", and "Claude reads this at its next turn" or "Claude has read this"; "See guidance" opens it), the properties (Status, Assignee, Counterparty, and Closes, Closed or Reopened) and the activity log ("Claude drafted an answer · 2m", "Raj added a comment · 1m").
  - "with {counterparty}" in the header hides when the view is narrower than 512px (beside an open Details panel), and when the pill already names them ("Waiting on Henderson Frame Lab").

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
- **Edges:** an edge appears when a thread opens and animates (moving dash) while a turn is in flight. The edge label carries the latest line, truncated ("The carbon-fiber tube adds $85 to the holder").
- **Side panel** (Cofounder), opened by clicking a node:
  - the org's name, kind and screening fit;
  - threads with this org (status, latest line, who's waiting). A closed thread shows the viewer's own side's closing summary, else why it ended, naming the org that closed it ("Northlake closed it"); an ended edge's label is that summary, else "Closed: {why}", else "Closed", and a thread the viewer can't open has no label. There is no "Expired";
  - "Act as Helen" to jump into that org's inbox when it needs a human.
- **Header strip:**
  - the request picker (which Harmony Network Request to show);
  - counts ("3 orgs · 2 threads · 1 active · 1 closed"), shown once the header is 640px wide; the picker's title gives way before them;
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
- **Intro** ("Meet Harmony", first visit only): the same shell with the spark, three sentences on what Harmony is, and one black **Next**, which starts the demo as Alex and opens What's on screen; closing that leaves the suggested message in the composer.
- **What's on screen** (the demo bar's round "?", at its right end): demo chrome over the whole app, all at once: a light scrim with each key control cut out and ringed in the accent colour, joined by a dashed curved arrow to a small dark callout in the demo bar's colours (under the bar's controls, right of the sidebar's Requests). One dark **Close this guide** button with a red ring sits in the middle of the screen; Esc closes it too. A click elsewhere on it, the scrim included, leaves it open (user, 2026-10-28).
- **Demo controls modal:** the same shell, with these sections:
  - Account (persona switcher);
  - World (reset; replaying the recorded big run arrives with the network world in P3);
  - Walkthrough (show suggested message, highlight suggested recipients);
  - Approvals: one switch, "Dangerously skip approvals", so no one in the world is asked (ARCHITECTURE §11.4);
  - Model and spend, read-only: which model each role runs on, and the session's live budget. Nothing here switches models.
- **Passcode page** (before the app loads; served by the server, so it isn't a Claude screen): the canvas color, a serif "Harmony" heading, one password field, an "Open" button, and inline errors: "That passcode isn't right." / "Too many attempts. Try again in N minutes."
- **Toast:** a small white pill at top right with an ⓘ icon, text and ×. Use it for "Northlake replied", "Switched to Dr. Helen Ward" and "Demo reset". When another tab switches account, this tab follows in place; if that dropped unsaved Customize edits here, the toast says so ("Your unsaved Customize changes were dropped when you switched to Raj Patel in another tab.").
- **Not-found pages** (round 2, `docs/mockups/round2/switching.html`): a chat or request the acting member can't open says whose it is, with a demo-chrome **Switch to Alex** (one per org, to the first person there who can open it) that lands back on it as them: "This chat is Alex Chen's" ("Chats belong to the person who started them. You're acting as …"), or "Raj can't open this request" with who it's between and who at each org can open it. An id this world doesn't have reads "This chat isn't in this demo" ("It's from before a reset or an earlier visit. Nothing from that demo carries over."), with "Go to your chats" or "Go to your requests".

## 10. Out of scope (popup on click)

Projects, Artifacts, Code, Search, Starred, Share, file upload, screenshots, Research, Web search, styles, voice, Language, Get help, Upgrade, Gift, Learn more, Billing, Privacy, Usage, Capabilities, Claude Code, Claude in Chrome, Skills, non-Harmony connectors, dark mode toggle (dark mode itself may come for free from tokens).
