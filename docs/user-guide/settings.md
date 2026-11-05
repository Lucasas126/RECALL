# user settings

The **Settings** button (gear, bottom of the left panel) opens the user settings dialog. They belong to the user, not to an agent, and are
shared with Telegram (`/settings`); agent options live in the [Agents](agents.md) tab. The dialog has a section list on the left (a strip
across the top on narrow screens); only one section shows at a time.

| Section | Settings |
| --- | --- |
| General | show model thinking; theme (this browser only, applied at once); language of alerts and errors; **Default agent** (the agent new chats open with when you do not pick one, and the Telegram router's fallback when it is unsure and no core agent is set; empty means the first agent in your list) |
| Models | default LLM model and default speech-to-text model (the model picker) |
| Chats & memory | maximum associative memory matches (1-20); days to keep disposable chats (1-365) |
| Telegram | choose the agent automatically (the orchestrator) and the core agent used when it is unsure (empty: *Same as default agent*) |
| Connections | your own account for each MCP server (see [MCP servers](mcp.md)); it has no Save button |

## defaults and reset

Each row shows its help text, the control, a muted **Default: X** line (the effective default, so a number says for example `3 matches`
and the allowed range) and a **↺** button (*Reset to default*) enabled only while the value differs from the default. Defaults are returned
by `GET /api/preferences` in a `defaults` object; see the [preferences API](../reference/user-yaml.md#preferences-api).

A setting you never changed is stored as nothing, and a reset stores nothing again: it saves `null` for the associative matches, the
disposable days, the orchestrator switch, the default agent, the core agent and the default models, so they keep following the application (or your
`user.yaml` models) if the default changes later. Theme, language and show model thinking always hold a value, so a reset puts the
default value back. The theme is kept in the browser and applies immediately; the other settings are applied by **Save settings**.

## numbers, saving and leaving

Numbers use a field with **−** / **+** buttons and a unit. Enter or leaving the field commits the value; a value outside the range is
moved to the nearest bound with a message under the field, and text that is not a whole number blocks saving.

Changes are saved with **Save settings**, which is enabled only when something changed. A line next to it counts the unsaved changes and a dot
on a section tab shows which section holds them; after saving, **Saved** appears and the dialog stays open. Closing the dialog (the close
button or Escape) with unsaved changes asks for confirmation first.
