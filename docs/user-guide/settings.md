# user settings

The **Settings** button (gear, bottom of the left panel) opens the user settings dialog. They belong to the user, not to an agent, and are
shared with Telegram (`/settings`); agent options live in the [Agents](agents.md) tab. The dialog has a section list on the left (a strip
across the top on narrow screens); only one section shows at a time.

| Section | Settings |
| --- | --- |
| General | show model thinking; show the agent's picture in replies (on by default); your **Profile picture**; theme and accent colour (**Appearance**, this browser only, applied at once); language of alerts and errors; **Default agent** (the agent new chats open with when you do not pick one, and the Telegram router's fallback when it is unsure and no core agent is set; empty means the first agent in your list) |
| Models | default LLM model and default speech-to-text model (the model picker) |
| Chats & memory | maximum associative memory matches (1-20); days to keep disposable chats (1-365) |
| Telegram | choose the agent automatically (the orchestrator) and the core agent used when it is unsure (empty: *Same as default agent*) |
| Connections | your own account for each MCP server (see [MCP servers](mcp.md)); it has no Save button |
| Users | only for a user whose maximum level is `admin` (the tab is hidden for everyone else); see [users](#users-administrators-only) |

## profile picture

**Settings > General > Profile picture** sets your own picture, shown instead of the first letter of your name at the bottom of the left
panel (32 px). **Upload image** takes a PNG, JPEG, WEBP or GIF file of up to 5 MiB and 4096 x 4096 pixels; the server turns it into a PNG of at
most 512 px on its longest side (EXIF rotation applied, metadata removed; SVG is refused). An animated GIF, WEBP or APNG stays animated: it is
stored as an animated WEBP of at most 100 frames, each of at most 512 px. **Remove** goes back to the letter.
**No circle or border** shows the image as it is, with no circle, border or background (meant for transparent PNGs); without it the image
is cropped to fill the accent-bordered circle. Like Appearance, these apply at once and do not count as unsaved changes; the style is
saved as `avatar_style` in `preferences.json`. The file is `data/<user>/.avatars/_user.png`. Agents get their own picture in the
[agent form](agents.md).

## defaults and reset

Each row shows its help text, the control, a muted **Default: X** line (the effective default, so a number says for example `3 matches`
and the allowed range) and a **↺** button (*Reset to default*) enabled only while the value differs from the default. Defaults are returned
by `GET /api/preferences` in a `defaults` object; see the [preferences API](../reference/user-yaml.md#preferences-api).

A setting you never changed is stored as nothing, and a reset stores nothing again: it saves `null` for the associative matches, the
disposable days, the orchestrator switch, the default agent, the core agent and the default models, so they keep following the application (or your
`user.yaml` models) if the default changes later. Theme, language, show model thinking and show the agent's picture always hold a value, so a reset puts the
default value back. The theme and the accent colour are kept in the browser and apply immediately; the other settings are applied by **Save settings**.

## numbers, saving and leaving

Numbers use a field with **−** / **+** buttons and a unit. Enter or leaving the field commits the value; a value outside the range is
moved to the nearest bound with a message under the field, and text that is not a whole number blocks saving.

Changes are saved with **Save settings**, which is enabled only when something changed. A line next to it counts the unsaved changes and a dot
on a section tab shows which section holds them; after saving, **Saved** appears and the dialog stays open. Closing the dialog (the close
button or Escape) with unsaved changes asks for confirmation first.

## appearance and art slots

**Settings > General > Appearance** holds three browser-only choices, applied at once and kept in `localStorage` (not in `preferences.json`, so
they are not shared with Telegram and do not count as unsaved changes):

- **Theme**: system, light or dark (the Theme row above it).
- **Accent colour**: pink (default), blue, gold, green, violet or cyan. It colours the primary buttons, the active-chat bookmark, links and
  focus rings. Each colour has a dark-theme and a light-theme value, and every text/background pair is at least 4.5:1 (WCAG AA); `tests/web/test_web_theme.py` checks it.

The page sets `data-accent` and `data-theme` on `<html>` before first paint (`static/appearance.js`, loaded as a normal
script, so no inline script is needed). Fonts (Fraunces, Inter, Geist Mono) are served from `static/fonts/`, so the page makes no request
to any other site; their licence (SIL Open Font License 1.1) is in `static/fonts/LICENSES.md`. The login art (arch and capital) lives in
`static/art/`; `static/art/README.md` lists each slot, its size and how to swap it.

## providers

The **Providers** tab holds what each provider needs from you:

- **Claude subscription**: shows whether you are signed in (and your plan). *Sign in* or *Sign in again* gives a link; open
  it, sign in to Claude and paste the code. Use it when the login expires. Needs `claude_config_dir` (see
  [Claude subscription](claude-subscription.md)). The chat error "Claude Code is not logged in" has an *Open Settings* button
  that opens this tab.
- **OpenRouter**: set your own API key. It is checked with OpenRouter before saving, stored under a variable of yours
  alone (`OPENROUTER_API_KEY_<USER>`) in `secrets.env` and never shown again; only your `user.yaml` is pointed at it. *Remove
  key* deletes it. Changes apply at once.

## users (administrators only)

The **Users** section of the settings dialog lists every user of the installation: name and id, maximum level (`max_level`, as a badge),
the number of agents and each agent with its level. A select and **Set maximum level** change another user's maximum
(`PUT /api/admin/users/<user_id>/max-level`); the change is written to that user's `user.yaml` and applies on their next request.
It is only shown to users whose own maximum is `admin` and the server checks it on every call (`403 admin_required` otherwise).

- You **cannot change your own maximum** (the select is disabled; ask another administrator).
- A maximum **below the level of one of the user's agents** is refused: the level an agent has, or the one its configuration needs when
  it declares none, counts. Picking such a level in the select lists the agents in the way and disables the button; the server answers
  `409 max_level_below_agents` with the same list. Lower those agents first (the user, or `python -m src.admin agent set`).
- Every change is logged by the web service (`administrator <id> changed the max_level of user <id>: <old> -> <new>`). The same can be
  done from the shell with `python -m src.admin user set --user U --set max_level=<level>`; `python -m src.admin user list` prints
  what this section shows. Sessions already open are not closed when a maximum is lowered, but every agent change is checked against the new value.

### Adding a user and onboarding them

**Add user** (administrators only) opens a four-step wizard in the Users section:

1. **Create the user**: id, name, language, maximum level (default `collaborator`) and a password. The same checks as `python -m src.admin create-user` apply. With the password field empty one is generated and shown **once** in the wizard; a typed password is never returned. A failed creation leaves no user folder behind.
2. **Provider access**: either write an OpenRouter API key for the user, or sign them in to Claude. The key is write-only: it is stored in `secrets.env` under a variable of that user alone (`OPENROUTER_API_KEY_<USER>`), the user's `user.yaml` names the variable, and the value is never returned, shown, logged or audited. It is accepted only in the request body. For Claude, the administrator opens the sign-in link and pastes the code; the user gets their own login folder in their config folder. The key is only format-checked (it is not sent anywhere).
3. **First agent**: pick a template and, optionally, a model from the list of that user's provider; the agent is created through the same path as the agent form.
4. **Review** of what was done.

Every step is an administrator-only route (`403 admin_required` otherwise) and writes one line without secrets to `<data>/<user>/admin_audit.jsonl` and to the log. The routes are in the [API reference](../reference/api.md#permission-levels-in-the-web-ui).
