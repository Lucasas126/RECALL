# MCP servers

RECALL agents can use external services through the
[Model Context Protocol](https://modelcontextprotocol.io/) (MCP). RECALL is the
MCP client: it starts or reaches each server, lists its tools and exposes them to
the model next to the built-in [tools](../reference/tools.md).

Two transports are supported:

- **stdio**: a local command (such as `npx`) started per user.
- **http**: a remote streamable HTTP endpoint, reached with each user's own
  access token.

## three levels

| level | where | what it controls |
| --- | --- | --- |
| framework | `mcp.servers` in the root `settings.yaml` | which servers exist, how they start, shared settings |
| user | Settings > **Connections** in the web UI | the user's own account/token for each server |
| agent | Agents > agent > **MCP** tab | which servers the agent uses, and how |

Every server in the catalog is visible to every user, but nothing is shared
between users: each user connects their own account. A connected user gets a
separate stdio server process, or a separate http session with their own token;
credentials and working directory live in `.config/<user_id>/mcp/<server_id>/`
(ignored by Git). Agents only get a server's
tools when the agent enables it **and** its user has connected it.

## catalog (`settings.yaml`)

```yaml
mcp:
  connect_timeout_seconds: 120  # server start and tool listing (1-600)
  call_timeout_seconds: 60      # one tool call (1-600)
  refresh_hours: 24             # idle connections list their tools again this often (0 = never)
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

### http servers

```yaml
mcp:
  servers:
    finance:
      name: Finance
      description: Read the user's accounts and movements.
      transport: http
      url: https://finance.example.com/mcp/
      auth:
        type: token
        label: Personal access token     # shown on the input field
        help: Create one in Finance > Settings > MCP access.
```

- `url` must be `https://`, or plain `http://` to a private-network host
  (`localhost`, `127.x`, `10.x`, `172.16-31.x`, `192.168.x`, a single-label name such
  as `atlas`, or a `.local`, `.lan`, `.internal` or `.home.arpa` name), so a token is
  never sent in clear text over the internet. Credentials in the URL are refused.
- `auth: {type: token}` (optional): each user pastes their own token, sent as
  `Authorization: Bearer <token>`. Without `auth` the server needs no connection.
- `command`, `args` and `env` are only for stdio servers; `url` and token auth only
  for http servers.

### local servers (`mcp_servers.yaml`)

Servers that belong to one installation (a service on your own network, for
example) do not need to go in the tracked `settings.yaml`. Put them in
`mcp_servers.yaml` inside the config folder (`paths.config_dir`, `.config/` by
default; ignored by Git and kept across image updates):

```yaml
servers:
  finance:
    name: Finance
    transport: http
    url: http://atlas:8000/mcp/
    auth: {type: token, label: Personal access token}
```

Only a `servers:` map is allowed, in the same format as above; it is added to the
servers of `settings.yaml`, and an id that already exists there is refused. The
timeouts stay in `settings.yaml`. A wrong file stops RECALL at start with a message
naming the problem.

Changes to `mcp_servers.yaml` are picked up without a restart; an invalid edit is logged and the previous catalog stays in use. An agent that lists a server missing from the catalog still loads, without that server. Changes to `settings.yaml` still need a restart.

## connecting an account

Settings > **Connections** lists every server with the user's status. **Connect**
runs the server's sign-in command for that user only; the browser on the RECALL
host opens the provider's consent page, and the dialog also shows an **Open sign-in
page** link. The dialog polls until the sign-in finishes. **Disconnect** stops the
user's server process and deletes their stored credentials. Users who do not want
an integration simply never connect it.

Because the sign-in callback goes to `localhost`, connect from a browser on the
computer that runs RECALL.

For an http server with token auth, the card shows a password field instead:
paste the token and press **Connect**. RECALL checks it against the server right
away and keeps it only if the server accepts it. It is stored in the user's
private folder (`token`, mode 600), never shown again and never put in a chat.
Paste a new one to replace it; **Disconnect** deletes it. If the server later
rejects the token (revoked or expired), tools report that the user must connect
again.

## enabling a server for an agent

The agent form has **Basic**, **Advanced**, **Tools** and **MCP** tabs. In **MCP**, each catalog server
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

- Server processes (or http sessions) start lazily on the first turn that needs
  them and are reused by all of that user's agents and chats. They stop when the web or Telegram
  service, or the CLI, exits (on Windows a Job Object also stops them if RECALL is
  killed). Server output is logged to `logs/<run>/mcp_<server>_<user>.log`.
- MCP tools are refreshed at the start of every turn, so connecting or
  disconnecting an account applies to open chats without restarting. Tools are
  named `<server_id>__<tool>` (for example `google_calendar__list-events`) and use
  the server's own JSON schema.
- A server's tool list is read when its connection opens and kept while the connection lives, so
  tools a server gains later do not show up by themselves. RECALL therefore restarts the
  connections that are idle every `mcp.refresh_hours` hours (default 24, `0` turns it off; the first
  round comes one interval after start-up, and a connection running a tool call waits for the next
  round). To pick up new tools at once, press **Refresh tools** next to a connected server in
  Settings > Connections or in the agent's MCP tab (`POST /api/mcp/<server_id>/refresh`); it answers
  409 while a tool call is running and shows the tool count or the error.
- Tool results are returned as `{"untrusted": true, "server": ..., "content": ...}`;
  the system prompt tells the model they are data, not instructions. Errors
  reported by the server, timeouts and crashed servers become tool errors (a
  crashed server is restarted on the next use).
- If an enabled server is unavailable (not connected, missing command or
  environment variable, failed start), the chat still works: the model is told the
  service is unavailable and why, and that the user can connect it in Settings.

Implementation: [src/providers/mcp.py](../../src/providers/mcp.py) (`McpManager`),
[src/agent/toolsets/mcp.py](../../src/agent/toolsets/mcp.py) and the
`/api/mcp` routes (see [configuration](agents.md#managing-agents)).

## Google Calendar

The catalog ships with the community
[`@cocal/google-calendar-mcp`](https://github.com/nspady/google-calendar-mcp)
server, run through `npx` (requires Node.js 20+ on the RECALL host; the Docker
image does not include it, see [deployment](../deployment/docker.md#limitations)). One Google
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
could be added as an http server once it accepts a bearer token.
