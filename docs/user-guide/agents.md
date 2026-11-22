# agents

An agent is an assistant with its own personality (`SOUL.md`), memory, tools and model settings. Each user owns any number of
agents and never sees another user's. How agents are isolated is explained in [agents and users](../architecture/agents-and-users.md);
the file format is in [agent.yaml](../reference/agent-yaml.md); the tools in [tools](../reference/tools.md).

A user with no agents sees an empty state with a **Create an agent** button.

The web **Agents** tab edits one agent in form tabs: **Basic** (an **Identity** group with name, description and, for a new agent, its
personality; a **Model and reasoning** group with the agent's default model, reasoning effort, reasoning on/off, keeping reasoning in
memory and the reasoning token limit; and a collapsed **Advanced** section with streaming, memory, RECALL knowledge, the
associative-memory options and messages kept in context),
**Tools** (use tools and the tool groups) and **MCP**; **Folders** holds the user folders and the memory index status. A saved agent also
has the dream tabs (**Soul & memory**, **Dream**, **Dream sessions**, see [dream mode](dream.md#web-ui)).

**Profile picture.** The Basic tab has a **Profile picture** group right after Identity: a preview, **Upload image**, **Remove** and a **No circle or border** checkbox (*For transparent PNGs: the image is shown as it is, without cropping.*). Pick a PNG, JPEG, WEBP or GIF file of up to 5 MiB and at most 4096 x 4096 pixels; it is uploaded at once and replaces the first-letter badge of the agent in the chat header (32 px) and is also shown large (about three lines tall, a bit smaller on phones) at the start of each of the agent's replies, with the text flowing beside it (turn this off with **Show the agent's picture in replies** in Settings > General); an agent without a picture shows nothing there. The server decodes the file, applies its EXIF rotation, scales it down so the longest side is at most 256 px, drops all metadata and stores a PNG (an animated GIF, WEBP or APNG stays animated: it is stored as an animated WEBP of at most 100 frames, each resized the same way; SVG is refused). By default the image is cropped to fill the accent-bordered circle; with **No circle or border** it is shown whole, with no circle, border or background, which suits transparent PNGs. The checkbox is saved with **Save agent** (`avatar_style` in [agent.yaml](../reference/agent-yaml.md)); upload and remove act immediately, and a new agent must be saved before it can have a picture. The picture is stored beside the agent's data folder, not inside it (`data/<user>/.avatars/<agent_id>.png` or `.webp` when animated), so the agent's own file tools cannot read or change it; renaming the agent from the admin CLI moves it, and deleting the agent keeps it on disk like the rest of its data.

**Hidden agents.** The Basic tab has a **Hidden** switch (saved with **Save agent** as `hidden` in [agent.yaml](../reference/agent-yaml.md#hidden)). A hidden agent is left out of the agent selectors (the one in the chat header, the default and core agent choices in Settings) but stays in the `@` list of the message box and keeps working as a mention or delegation target. The Agents list has two sections, **Agents** and **Hidden agents** (with a hint while it is empty); hidden agents carry an eye-off icon there and in the editor header. Every agent's editor, hidden or not, has an **Open chat with this agent** button that starts a new, normal chat with it and switches to the chat view. Hiding is only about the lists, not a security boundary.

**Hidden chats.** The chat an @mention opens with the mentioned agent is hidden from the chat list. Turn on **Show hidden chats** under the **New chat** button to list them (they carry an eye-off icon); the choice is remembered in this browser and is off by default. Hidden chats are still searched, indexed and used by Dream, and writing into one keeps it hidden.

**Agents managing agents.** An agent of level `operator` or above can hold the **Manage agents** tool group (Tools tab; shown with the level it needs, like **Manage permissions**): it lists, creates, edits, renames and deletes your agents (never their level, tool groups or approval rules, which belong to **Manage permissions**) and every change shows an approval card you approve once or deny. It acts only on your agents, never on one above its own level, and never renames or deletes itself. The `guide` template has both groups ([tools](../reference/tools.md#agents), [permissions](../architecture/permissions.md#implemented-step-8-the-agents-tool-group)).

**Permission level.** The Basic tab starts with a **Permission level** group (see [permission levels](../architecture/permissions.md)).
The select lists the six levels (Minimal to Admin) with the description of the selected one, built from the same table the server
enforces; the options above your maximum level (the `max_level` an administrator gave you) are disabled. Next to it the form shows
**the level the current choices require**, computed live from the Tools tab (tool groups), the MCP tab, the terminal backends that are
allowed (a `local` or root ssh backend needs `admin`, any other ssh backend `operator`) and the delegation targets, and says what
is wrong when the selected level does not fit: *raise the level to X or remove Y* when your maximum allows it, *remove Y or ask an
administrator to raise your maximum* when it does not. In the Tools tab a group that needs more than the selected level is marked
with a badge showing the level it needs (the tooltip explains; a red badge means above your maximum); groups are never hidden. A
new agent starts at the level its choices need and, until you pick one yourself, follows them; the form sends `level` only when you
chose it (or the choices moved it), otherwise the stored level, or none, is kept. A badge with the level appears on each agent in
the list and next to the title. The server stays the authority: a refused save shows `level_exceeds_max`, `level_below_required` or
`level_required_above_max` next to the selector, in your language.

Reasoning is on by default for new agents (and for agent files that omit `reasoning`); turn it off in **Basic > Model and reasoning > Reasoning**.
Models without reasoning support ignore the request. When it is off, the composer's effort selector reads **Disabled in this agent**.

The **Delegation** section of the Tools tab appears while the **Delegate** tool group is ticked: whether the agent may hand tasks to workers, which tool groups workers may get, the parallel, timeout and token limits (each with its default and a reset button), and the agents it may hand tasks to, each with **Can write its own memory**. See [delegation](delegation.md).

## defaults and reset

Every setting in the agent form (the Basic, Tools and Dream tabs) is one row: its name and a short help line, the control, a muted
**Default: X** line and a **↺** button (*Reset to default*) that is enabled only while the value differs from the default. The defaults
come from the configuration models through `GET /api/agents/defaults`, so they match what the server applies to a field an agent file
omits. For the few limits that an empty field leaves to the application (tool rounds per turn and the delegation limits), the default shown is the live value from `settings.yaml` (for example *Default: 50 rounds*, or *no limit* for 0 tokens) and *Reset* empties the field so the agent keeps following the application. Otherwise resetting puts the default value in the field; **Save agent** (or **Save dream settings**) stores it. The default model resets to
*your default model*, the reasoning token limit to *no limit*, and the dream schedule to *manual only*.

Numeric fields (messages kept in context, reasoning tokens, command timeout, dream steps, tokens, minutes and the schedule interval) are a
text field with **−** / **+** buttons, a unit and the allowed range (taken from the same models). Type a number or use the buttons or the
up/down arrow keys; Enter or leaving the field commits it. Big numbers show thousands separators. A value outside the range is moved to the
nearest bound with a message under the field, and text that is not a whole number is flagged and blocks saving. **Messages kept in
context** keeps all messages when empty; its default is 30.

## managing agents

The button at the top left of the chat header (and of the Agents view) hides or shows the left panel so the chat can use the full width; the choice is remembered in this browser, and on narrow screens the same button opens the menu drawer.

On desktop the chat header also shows the open chat's name after the agent's name (a chat without messages shows *New chat* and cannot be renamed yet). Click it to rename the chat in place: Enter or leaving the field saves, Escape cancels and an empty name is ignored; it uses the same rename as the side panel, so both stay in sync. Drag the bottom edge of the header (or focus it and use the up and down arrow keys; double-click resets) to make the header taller up to 220 px: the agent's picture grows with it, and the height is remembered in this browser. On narrow screens the header is unchanged and does not show the chat name.

the web **Agents** tab lists the user's agents in the sidebar (name and description) and edits the name, `description`, all `chat_config` options (associative memory is a mode selector plus a timing selector, the latter disabled while the mode is off) and, in the **Folders** tab, the folder list (name, path, indexed, read-only) with the agent's memory index status (sources, files, chats and chunks indexed, last reconciles, rebuilding or indexing) and **Reindex** / **Rebuild from scratch** buttons. a new agent's `id` is derived from its name (lowercase, accents removed, non-alphanumeric characters replaced by `-`) and is not editable from the form. the same operations are available as routes (see the [API reference](../reference/api.md#agent-routes)).

## writing agent descriptions

The `description` (0-500 characters) is also what the [Telegram orchestrator](telegram.md#telegram-orchestrator-automatic-agent-choice)
reads to choose an agent, so write one or two sentences saying which topics the agent should answer. It is sent, with
the agent's name, to the decision model's provider; the agent's chat model never sees it.

- Good: `Personal finance: budgets, taxes, invoices, savings and investments.` / `Cooking and nutrition: recipes, meal plans, shopping lists.`
- Weak: `My helper` or `General assistant` (it matches everything, so choices become uncertain).
- Agents with overlapping topics need descriptions that tell them apart, for example `Work email: replies, summaries and scheduling for the office` versus `Personal email: family, friends and bills`. Without it the orchestrator cannot separate them and falls back to the core agent.
- An agent without a description is judged by its name only.

Check the result with `python -m src.admin route-test "how much tax do I owe?" --user <id>` (set `OPENROUTER_API_KEY`; add `--force` if the orchestrator is not enabled yet): it prints the ranked agents, probabilities, confidence and latency.

## rules

rules:

- ids match `[A-Za-z0-9_-]{1,64}`; `name` has 1–100 characters and `description` 0–500 (optional, empty by default). the agent body has no personality field (an unknown field such as `personality_prompt` is a `422`): the personality is `SOUL.md`, read and written through `/api/agents/<agent_id>/soul`. a new agent starts with a short default `SOUL.md` (the packaged template `src/agent/default_soul.md`, written only when the agent has no `SOUL.md` yet, so a reused data folder keeps its own); the form's personality field, when filled, replaces it right after the agent is created (the template stays in the SOUL history). agents created before this feature are not changed: while their SOUL editor is empty, **Soul & memory** shows an **Insert default template** button that fills the editor (save to apply it).
- ids are unique per user. a duplicate id, or an existing file name, returns `409`.
- changing the `id` through `PUT` (the web form never does) moves `data/<user_id>/<old_id>` to `data/<user_id>/<new_id>` and updates the user's last used, default and core agent preferences that pointed at it. if the agent file was named after its old id, it is renamed after the new one; otherwise it keeps its name. if the data folder cannot be moved (for example, another process is using it), the change is rolled back.
- deleting keeps `data/<user_id>/<agent_id>` on disk, so creating an agent later with the same id reuses that memory. if the deleted agent was the last used one, that preference falls back to the user's first agent; a deleted default or core agent is cleared (the default then means the first agent in the list).
- editing, renaming or deleting an agent with active or queued replies returns `409`; the cached runtime is rebuilt from the saved file on the next message.
- writes are atomic and serialized per user by `.config/<user_id>/agents.lock`. only the `agents:` entry of `user.yaml` is rewritten, so comments and other keys are kept.
- Telegram sessions that are already open keep the agent id they started with; use `/selectagent` (or restart the bot) after a rename or delete.

reloading the page reopens the chat and the agent you had open (the address holds `#chat/<agent id>/<chat id>`, and the last chat of each user is remembered in the browser as a fallback for a new tab). if that chat or agent no longer exists, a new chat starts. logging out forgets it.

you can switch to another chat, or reload the page, while a reply is being generated: the reply keeps running on the server, and when you return to that chat (or open it in another tab) the web UI shows what was generated so far and keeps updating live until it ends. the chat list marks chats that are still replying.

a long chat opens at its end: the web UI draws the last 25 messages and puts a bar above them saying how many earlier ones are not shown (`150 earlier messages not shown (of 210)`) with **Load 25 more** and, when more than 25 are hidden, **Load all**. loading older messages keeps what you are reading where it is, and when the page reloads the chat after a reply or an edit it asks for as many messages as you had opened, so a longer history stays open. messages you edit keep working the same way, whether they were loaded first or later. (the full chat is always on the server; the API reads it in pages, see the [API reference](../reference/api.md).)
