# RECALL knowledge (built-in skill)

Every agent can read a built-in manual about how RECALL itself works: the **`recall`** skill. It is how an agent answers
"where do my memories live?", "why is my terminal denied?" or "what does this setting do?" without guessing.

- **What it is.** A hub (`SKILL.md`, a router plus hard rules) and reference pages (architecture, configuration fields, tools,
  memory and dream, terminal and approvals, troubleshooting, code map). The agent sees one line in its skills index and loads a
  page with `skill_view("recall", "references/<file>.md")` only when it needs it.
- **Read-only.** The agent cannot edit, delete or shadow it: `skill_manage` on `recall` is refused with a message telling it to
  create a skill of its own with another name, and an own skill named `recall` cannot be created. Reads always work, also in
  disposable chats and dream sessions. The agent's own skills (`data/<user_id>/<agent_id>/skills/`) are unchanged and editable.
- **Switch.** On by default for every agent, new and existing. Turn it off per agent in the Agents tab (**Basic > Advanced >
  RECALL knowledge (how RECALL works)**) or with `chat_config.recall_knowledge: false` ([agent.yaml](../reference/agent-yaml.md)). Off means
  no index line and no `skill_view` access to it. The change reaches new chats.
- **Always in sync.** The pack lives in the code (`src/knowledge/recall/`) and ships in the Docker image, so it matches the running
  version; its frontmatter carries the version from `pyproject.toml`. It is not copied into the agent's data folder and is not
  part of the memory index.
- **Prompt cost.** One index line ("Built in (read-only)"); the body is loaded on demand.

## Extending it

Edit the Markdown under `src/knowledge/recall/`: keep the hub under 6 KB and each reference under 8 KB, write for the agent (what
to do, which file, which key), and state only what the code or the docs confirm. Add every new reference file to the hub's router
table. `tests/test_recall_knowledge.py` checks that links resolve, that every real tool and every config field is documented and no
unknown tool is named, that cited source paths exist, and that the pack ships in the image. When you change behavior, config fields,
tools or commands, update the pack in the same change.
