# MCP servers

RECALL agents can use external services through the
[Model Context Protocol](https://modelcontextprotocol.io/) (MCP). RECALL is the
MCP client: it starts each server, lists its tools and exposes them to the model
next to the built-in [tools](tools.md).

Status: implemented for stdio servers (a local command such as `npx`). Remote
(streamable HTTP) servers are planned.

## three levels

| level | where | what it controls |
| --- | --- | --- |
| framework | `mcp.servers` in the root `settings.yaml` | which servers exist, how they start, shared settings |
| user | Settings > **Connections** in the web UI | the user's own account/token for each server |
| agent | Agents > agent > **MCP** tab | which servers the agent uses, and how |

Every server in the catalog is visible to every user, but nothing is shared
between users: each user connects their own account, and each connected user gets
a separate server process whose credentials and working directory live in
`.config/<user_id>/mcp/<server_id>/` (ignored by Git). Agents only get a server's
tools when the agent enables it **and** its user has connected it.

## catalog (`settings.yaml`)

```yaml
mcp:
  connect_timeout_seconds: 120  # server start and tool listing (1-600)
  call_timeout_seconds: 60      # one tool call (1-600)
  servers:
    google_calendar:            # id: lowercase letters, digits and _, max 32 characters
      name: Google Calendar
      description: Read and manage events in the user's Google Calendar.
      command: npx
      args: ["-y", "@cocal/google-calendar-mcp@2.8.1"]
      env:
        GOOGLE_OAUTH_CREDENTIALS: {env: GOOGLE_OAUTH_CREDENTIALS}
        GOOGLE_CALENDAR_MCP_TOKEN_PATH: {user_file: tokens.json}
      exclude_tools: [manage-accounts]
      auth:
        args: ["-y", "@cocal/google-calendar-mcp@2.8.1", "auth"]
        credential_file: tokens.json
```

- `command` is resolved on the RECALL host's `PATH`; a missing command is reported
  to the user and the agent.
- `env` values are a literal string, `{env: NAME}` (copied from RECALL's own
  environment, shared by all users; an unset variable is an explicit error) or
  `{user_file: name}` (absolute path of a file in the user's private folder). Only
  these variables plus a minimal safe set (`PATH`, `APPDATA`, ...) reach the
  server; RECALL's other secrets do not. Never put secrets in YAML.
- `exclude_tools` hides server tools from every agent.
- `auth` (optional) describes the server's own sign-in command. The user counts as
  connected when `credential_file` exists in their folder. Servers without `auth`
  need no connection. `url_pattern` (default `Authentication URL:\s*(https?://\S+)`)
  finds the sign-in URL in the command output, and `timeout_seconds` (30-1800,
  default 300) ends an unfinished sign-in.

Restart RECALL after changing the catalog.

## connecting an account

Settings > **Connections** lists every server with the user's status. **Connect**
runs the server's sign-in command for that user only; the browser on the RECALL
host opens the provider's consent page, and the dialog also shows an **Open sign-in
page** link. The dialog polls until the sign-in finishes. **Disconnect** stops the
user's server process and deletes their stored credentials. Users who do not want
an integration simply never connect it.

Because the sign-in callback goes to `localhost`, connect from a browser on the
computer that runs RECALL.

## enabling a server for an agent

The agent form has **General** and **MCP** tabs. In **MCP**, each catalog server
can be enabled, and then configured with:

- **Read only**: expose only tools the server annotates as read-only
  (`readOnlyHint`).
- **Instructions for the agent**: text added to the system prompt for this server.
- **Tools**: all tools (including ones the server adds later) or a fixed
  selection. The list is loaded from the server, so the user must be connected.

The selection is stored in the agent YAML:

```yaml
chat_config:
  tools: true                  # also required for MCP tools
  mcp:
    google_calendar:
      tools: null              # null = all; or a list such as [list-events, get-event]
      read_only: false
      instructions: Use my "Work" calendar unless I say otherwise.
```

Saving an agent that enables an unknown server id is rejected. Loading an agent
whose YAML references a server no longer in the catalog is an error.

## runtime behavior

- Server processes start lazily on the first turn that needs them and are reused
  by all of that user's agents and chats. They stop when the web or Telegram
  service, or the CLI, exits (on Windows a Job Object also stops them if RECALL is
  killed). Server output is logged to `logs/<run>/mcp_<server>_<user>.log`.
- MCP tools are refreshed at the start of every turn, so connecting or
  disconnecting an account applies to open chats without restarting. Tools are
  named `<server_id>__<tool>` (for example `google_calendar__list-events`) and use
  the server's own JSON schema.
- Tool results are returned as `{"untrusted": true, "server": ..., "content": ...}`;
  the system prompt tells the model they are data, not instructions. Errors
  reported by the server, timeouts and crashed servers become tool errors (a
  crashed server is restarted on the next use).
- If an enabled server is unavailable (not connected, missing command or
  environment variable, failed start), the chat still works: the model is told the
  service is unavailable and why, and that the user can connect it in Settings.

Implementation: [src/providers/mcp.py](../src/providers/mcp.py) (`McpManager`),
[src/agent/toolsets/mcp.py](../src/agent/toolsets/mcp.py) and the
`/api/mcp` routes (see [configuration](config.md#managing-agents)).

## Google Calendar

The catalog ships with the community
[`@cocal/google-calendar-mcp`](https://github.com/nspady/google-calendar-mcp)
server, run through `npx` (requires Node.js 20+ on the RECALL host). One Google
OAuth client is shared by the framework; each user signs in with their own Google
account and gets their own token file.

One-time setup by the RECALL administrator:

1. In the [Google Cloud console](https://console.cloud.google.com), create or
   select a project and enable the Google Calendar API.
2. Configure the OAuth consent screen (Google Auth Platform). While the app is in
   **Testing**, add every RECALL user's Google address as a test user; Google then
   expires tokens after 7 days, so users reconnect weekly. Publishing the app
   avoids this.
3. Create an OAuth client of type **Desktop app** and download its JSON file.
   Store it outside the repository (it contains the client secret).
4. Add the file's path to the ignored root `.env`:

   ```dotenv
   GOOGLE_OAUTH_CREDENTIALS=C:\path\to\gcp-oauth.keys.json
   ```

5. Restart RECALL. Each user then connects Google Calendar in Settings >
   Connections, and enables it per agent in the **MCP** tab.

The server's `manage-accounts` tool is hidden because accounts are connected from
RECALL. Google also offers an official remote Calendar MCP server
(`calendarmcp.googleapis.com`), currently in Developer Preview and HTTP-only, so it
is not supported yet.
