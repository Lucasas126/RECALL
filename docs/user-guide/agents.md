# agents

An agent is an assistant with its own personality (`SOUL.md`), memory, tools and model settings. Each user owns any number of
agents and never sees another user's. How agents are isolated is explained in [agents and users](../architecture/agents-and-users.md);
the file format is in [agent.yaml](../reference/agent-yaml.md); the tools in [tools](../reference/tools.md).

A user with no agents sees an empty state with a **Create an agent** button.

The web **Agents** tab edits one agent in form tabs: **Basic** (name, description and, for a new agent, its personality, plus a collapsed
**Advanced** section with streaming, memory, reasoning, associative-memory options, retained messages and the default model),
**Tools** (use tools and the tool groups) and **MCP**; **Folders** holds the user folders and the memory index status. A saved agent also
has the dream tabs (**Soul & memory**, **Dream**, **Dream sessions**, see [dream mode](dream.md#web-ui)).

Reasoning is on by default for new agents (and for agent files that omit `reasoning`); turn it off in **Basic > Advanced > Reasoning**.
Models without reasoning support ignore the request. When it is off, the composer's effort selector reads **Disabled in this agent**.

## managing agents

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
- changing the `id` through `PUT` (the web form never does) moves `data/<user_id>/<old_id>` to `data/<user_id>/<new_id>` and updates the user's preferred agent. if the agent file was named after its old id, it is renamed after the new one; otherwise it keeps its name. if the data folder cannot be moved (for example, another process is using it), the change is rolled back.
- deleting keeps `data/<user_id>/<agent_id>` on disk, so creating an agent later with the same id reuses that memory. if the deleted agent was the preferred one, the preference falls back to the user's first agent.
- editing, renaming or deleting an agent with active or queued replies returns `409`; the cached runtime is rebuilt from the saved file on the next message.
- writes are atomic and serialized per user by `.config/<user_id>/agents.lock`. only the `agents:` entry of `user.yaml` is rewritten, so comments and other keys are kept.
- Telegram sessions that are already open keep the agent id they started with; use `/selectagent` (or restart the bot) after a rename or delete.
