# models and providers

How models are chosen: the user's providers, model references, the default models, the agent default, the per-chat
override, reasoning effort, favorites and zero data retention (ZDR). The `user.yaml` keys themselves
(`provider`, `models`, `require_zdr`, ...) are in the [user.yaml reference](../reference/user-yaml.md); the HTTP routes are in the
[API reference](../reference/api.md). The experimental Claude login provider has its own page,
[Claude subscription](claude-subscription.md).

Precedence of the chat model: **model chosen in the chat > agent `chat_config.model` > user default `models.chat`**.

## providers

There is no per-user "enable a provider" setting. The **model picker** (chat,
Settings → default LLM/STT, Dream tab) has a provider switcher at its top, one tab
per provider able to serve that kind of model: OpenRouter, Ollama and the
experimental Claude subscription for chat and dream models; OpenRouter only for
speech-to-text. For chat and dream models a first **Any** tab (usable when at
least one provider is) lists the models of every available provider together,
each row tagged with its provider; unavailable providers are left out, and if
one provider's catalog fails while others answer, the others still show. Choosing
a row in Any is the same as choosing it in its own tab (the stored reference is
`<provider>:<model>`), favorites show all providers' favorites, and the ZDR
checkbox filters the OpenRouter rows only. A provider tab lists only that
provider's models. The picker opens on **Any** if that was the last tab the user
chose (kept in the browser, per user); otherwise on the provider of the currently
selected model, else the last provider used, else the main provider. Switching
tab never changes the *favorites only* filter. Embeddings always use the main
provider (no picker).

A provider is **available** when the server can use it, checked on the server:

| provider | available when | otherwise the tab is disabled with |
| --- | --- | --- |
| OpenRouter | its API key environment variable is set (never read back) | `API key variable not set` |
| Ollama | the server answers at its host | `Ollama not reachable` |
| Claude subscription | the user has their own `claude_config_dir` or is listed in `claude_subscription.default_login_users` (`settings.yaml`), the `claude` CLI is found and `claude auth status` says it is logged in | `Claude Code not installed` / ``Run `claude auth login` `` |

Slow checks (Ollama, Claude Code) are cached for 60 seconds per server, so opening
the picker does not start the CLI each time; the key check is a plain lookup. The
Claude tab is labelled *experimental* (and *experimental, not ZDR* when the user
requires ZDR). The ZDR checkbox appears for OpenRouter only; Ollama and Claude
subscription models do not use it. Telegram has
no provider switcher: its list is the user's favorites from all available
providers, each line tagged with its provider.

## user model defaults and chat overrides

All model IDs belong to the user. User YAML supplies initial defaults:

```yaml
models:
  chat: openai/gpt-4o-mini
  stt: openai/whisper-large-v3-turbo
  embeddings: openai/text-embedding-3-small
```

Persistent user preferences can override `default_llm_model` and
`default_stt_model`; null restores the corresponding user YAML value.
Setting an LLM default validates the chat catalog under the user's ZDR policy
(`require_zdr`); setting an STT default validates the transcription catalog under
the speech-to-text policy.
Defaults are independent of agent selection. Embeddings remain a user YAML
setting, not an editable inference preference.

New chats have no LLM selection: `model_override` is null. Effective LLM
models resolve as **explicit chat override > agent default model > current user
default**. An agent sets its default with the optional `chat_config.model` key of
its YAML (a full reference such as `claude_subscription:sonnet` or
`anthropic/claude-haiku-5.5`; unset follows the user default, which is what every
existing agent does). It applies to new chats and to old chats without a
selection; a chat's own selection keeps winning. The picker entry that clears a
chat selection reads **Use agent default** when the agent has one, else **Use
user default** (web and Telegram). The agent default is edited in the web UI
(Agents > Advanced > Default model, same picker with provider tabs; **Clear**
follows the user default), not in Telegram. Saving validates it like a chat model
(provider usable, model offered, ZDR catalog filter) and rewrites the agent file
through the agent store, which keeps all other keys but not YAML comments.
With `require_zdr` a `claude_subscription:` agent default is refused (it has no
ZDR and an agent default has no `zdr` override): saving fails, and a file edited
by hand fails the replies of chats that inherit it with a clear message. A retired
model behaves as a retired user default. Dream with no `dream.model` follows the
agent default; chat naming and associative memory keep their own models. The STT
model has no per-chat override; it is always the current user default.
Changing a default affects subsequent requests in every inheriting chat,
including existing chats, without changing chats with explicit overrides or
any generation already running. Cached agents refresh inherited inference
models before the next generation, invalidate model-specific token estimates,
and retain their existing embedding index. CLI requests refresh the same user
defaults. Agent YAML has no `models` block and rejects one.

`GET /api/agents/<agent_id>/chats/<chat_id>` includes the effective `model`,
the user's `stt_model` default (read-only here), and a nullable `model_override`.
Its `/models` GET endpoint returns only those model fields (plus
`model_source`: `chat` | `agent` | `user`, the level the effective model comes
from, `agent_default_model` and `user_default_model`) and the reasoning
effort state, without loading conversation history or initializing an agent.
The chat `/model` PUT endpoint accepts `{"model_id": null}` to clear an override
without changing user defaults. Changing it requires an idle agent. Audio requests
validate chat existence before resolving STT or contacting the provider;
missing/invalid chats return 404, including deletion during transcription.

Remove old agent `models` blocks and configure model IDs in user YAML instead.
There is no legacy configuration compatibility or migration utility.
Existing chat LLM sidecars remain explicit overrides. Legacy `<chat_id>.stt.json`
files are no longer read and are removed with their chat.

Both channels expose user defaults: Telegram `/settings` saves model choices
immediately; web **Settings** stages choices until **Save settings**, including
when no chat is open. Their selectors follow the user's ZDR policy; web chat LLM
overrides can still select the other policy.

Telegram `/selectmodel` and the web composer's chat model selector change only
the current chat. The STT default is changed only in **Settings** (web or Telegram). **Use user default** clears the
corresponding override. Selectors distinguish **User default**, **Agent default** and **Chat
override** and show the effective model. Web settings changes from Telegram
refresh at the existing preference-refresh points without overwriting explicit
chat selections. With an open chat, the web also reads current model metadata
after refreshing or saving preferences, so overrides selected or cleared in
Telegram replace stale display state. It does not reload messages; responses
for replaced sessions/chats or superseded local model selections are ignored.
Default changes can be saved while a reply runs; changing chat
overrides still requires an idle agent.

Status: schemas, service/API precedence, user-default controls and chat reset
controls are implemented in Telegram and web. The default speech-to-text model
must be an OpenRouter model; default chat models can come from any available
provider.

## openrouter model catalog and zero data retention

ZDR is optional and off by default (`require_zdr: false`, see
[above](../reference/user-yaml.md#zero-data-retention-is-a-user-policy)). While it is off, RECALL sends no
`provider.zdr` field at all (neither `true` nor `false`), so the OpenRouter account's
own policy decides, and it uses the plain public catalog and base prices.

When ZDR applies (`require_zdr: true`, a feature's own `zdr: true`, or a chat that
opted in) OpenRouter chat requests carry `provider: {"zdr": true}`. This applies to
streaming, non-streaming, transport retries and auxiliary chat calls such as
associative-memory query generation. OpenRouter must reject requests with no
eligible ZDR route; RECALL never relaxes it automatically after a failure.
A chat can deviate from the user's setting by selecting a model with the selector's
ZDR checkbox changed, which persists `zdr: true` or `zdr: false` for that chat. The
flag is passed per request, including auxiliary queries and retries; it is not
mutable state shared by providers or chats. New chats and old metadata without the
flag follow the user's `require_zdr`; metadata with a flag keeps it. Only a choice
that differs from the user's setting is stored. OpenRouter account/guardrail
policies can still require ZDR even when ZDR is off in RECALL.
Ollama accepts but ignores this routing preference; embeddings are unchanged.

The web backend exposes `GET /api/models?q=<name-or-id>&zdr=true` for the current user:
the chat rows of `GET /api/models/catalog` (see [providers](#providers)), merged
over the available providers; `zdr` filters only the OpenRouter rows and defaults to
the user's `require_zdr` when omitted. It fetches
the public `/api/v1/models` and `/api/v1/endpoints/zdr` catalogs without sending
credentials, conversation content or the search term to OpenRouter. Results
contain `id`, `name`, `description`, sorted, deduplicated `zdr_providers`, and
`pricing: {"prompt": "0.1", "completion": "0.5"}`.
Prices are decimal strings in **USD per million input/output tokens**,
calculated independently as the minimum known nonnegative base price among
that model's active ZDR routes when ZDR applies, not an unrestricted cheaper route.
Without ZDR, prices come from the public model catalog's base pricing.
Missing or negative (unknown/dynamic) upstream prices become `null`, displayed
as unavailable rather than free. Zero is a valid free price. The selector
shows both rates with a "from" label, including in the favorites view.
These are indicative base rates, not a quote: routing, context-length tiers,
caching, account discounts and additional charges can change the final cost.
Only text-output models are returned, sorted by name. When ZDR applies, an active
(`status: 0`) ZDR endpoint is also required; otherwise models without one are included. `zdr_providers` always describes known active ZDR routes,
not the actual route a later request will use. Search matches model names or IDs, case-insensitively.
An empty result is valid; failed or invalid upstream responses produce an
explicit HTTP 502, never an unfiltered fallback. The catalog is fetched on
each request and describes public routes, not account-specific availability,
credits, access or a guarantee that a route will remain available.

ZDR here means [OpenRouter's policy](https://openrouter.ai/docs/guides/features/zdr):
it permits in-memory prompt caching and includes legal/CSAM retention exceptions.
It is not a blanket no-retention guarantee for RECALL, local history/logs,
embeddings or external tools such as Tavily. No OpenRouter plugins are enabled
by this integration.

## choosing a model per chat

The **Chat model** control in the web composer (above the message box) opens a
model selector, grouped by provider (main provider first, each group labelled), with name/ID
search, ZDR provider names (while ZDR applies) and a Refresh button. A star beside the search
field toggles Favorites only; its filled/highlighted state means the filter
is active. The ZDR checkbox remains visible, and capability checkboxes live in the collapsed
**Filters** disclosure with an active-filter count. Closing it keeps filters
applied; each selector opening collapses it again. **Information** holds the
longer policy/capability notes; the non-ZDR retention warning appears only when
the user requires ZDR and unchecks the box. The favorites toggle supports Enter/Space and exposes
its pressed state to assistive technology; both disclosures use native keyboard
controls.
The **ZDR only** checkbox opens in the user's policy (unchecked unless
`require_zdr` is true), regardless of the current chat's policy or a previous
search. Checking it fetches the ZDR-filtered catalog; when the user requires ZDR,
unchecking it fetches the unrestricted catalog and shows a retention warning.
Editing the search text does not reset the checkbox. Opening the selector or
changing its filter does not change the chat's policy: choosing a model does.
The composer shows no ZDR text while the user does not require it and the chat has not
opted in; otherwise it shows "ZDR required" or "ZDR not required" for the current chat.
Saved favorites appear first. The Favorites only filter is always off when the selector
opens. Favorites missing from the current catalog are visible but cannot
be selected; they can still be removed. The browser keeps the catalog until
refresh, a filter change or logout, but additions and selections are revalidated
on the server. An unrestricted catalog is never reused as a ZDR-filtered one.

The selector's **Sort** menu defaults to Favorites first and also offers
Name A-Z and input/output prices in either direction. Price ordering uses the
unrounded numeric rates for the current catalog/filter, not their displayed
currency strings. Unknown prices always sort last, including when descending;
zero-priced models are valid. Price and name modes do not pin favorites first.
Ties sort by name, then ID. Sorting combines with search and Favorites only
without changing ZDR or chat settings. The choice lasts until logout/reload;
ZDR still resets to the user's policy whenever the selector opens.

Capability checkboxes filter for **Tools**, **Image input** and **Reasoning**.
All selected capabilities must match. Tools requires `tools`; reasoning requires
`reasoning` or `reasoning_effort` in the supported parameters. With ZDR checked,
one active ZDR endpoint must advertise both when both are selected; capabilities
from separate routes are not combined. Without ZDR, the public model's supported
parameters are used. Image input uses the model's declared `input_modalities`,
not an endpoint-specific guarantee. Missing metadata does not satisfy a filter.
Filters combine with search, favorites and ordering and reset on logout/reload.
They do not change the chat routing policy, enable agent tools/reasoning, or
pin an upstream provider. The web composer accepts image, audio and video
attachments and PDFs; PDFs are converted to text on the server (see [pdf input](files-and-media.md#pdf-input))
and need no model support. Each selected model must explicitly declare the corresponding
`input_modalities` before RECALL sends the file, except that videos are
decomposed for image-only models (see video input). Files are limited to 10 MB
each and 25 MB per message. Audio is passed directly to models that advertise
audio input; automatic speech-to-text is not configured. Prices remain catalog base minima under the ZDR filter, not quotes
for a capability-filtered route. The API includes `input_modalities` and
`parameter_routes` (one supported-parameter list per active ZDR endpoint, or a
single model-level list when ZDR is unchecked) for these client-side filters.

Selecting a model changes **only the current chat**, preserving its messages
and leaving user defaults, other chats and embeddings unchanged. New chats
inherit the user's current default without an explicit override. A selection survives browser/server restarts
and is also honored when the CLI opens that chat. The override stores the model
reference; a bare id is served by the user's main provider when the chat runs (see
[model references](../reference/user-yaml.md#model-references)), and a chat whose provider is not available
fails with `provider not available: <provider>: <what to fix>`.

The **ZDR only** filter applies to OpenRouter rows. Ollama rows have no per-token
price; they are local models except Ollama cloud models (tags ending in `cloud`),
which run on Ollama's servers and are labelled cloud (and not ZDR when the user requires ZDR). With the experimental [Claude subscription](claude-subscription.md)
available, `claude_subscription:haiku`, `:sonnet` and `:opus` are listed in their own
group whatever the ZDR filter, labelled experimental (and not ZDR when the user requires ZDR), without per-token
prices. Choosing one stores the chat override without a `zdr` flag, or as
`{"provider": "claude_subscription", "id": "claude_subscription:sonnet", "zdr": false}`
when the user requires ZDR. The composer shows "Claude subscription" (plus ", not ZDR" when the user requires ZDR; no warning style, and the *experimental* note lives on the picker tab only). The default
LLM picker and the Dream tab's **Browse** use this dialog; Telegram `/selectmodel`
and the default model in `/settings` list the user's favorites from all available
providers instead (no search). The default STT picker lists OpenRouter only. Telegram says nothing about ZDR unless the user requires it (or the chat opted in);
then it never says "ZDR: enabled" for a Claude subscription model but "Not ZDR (Claude
subscription, experimental)" (or the Spanish equivalent). Its lists tag Claude rows
with 🧪 and "plan" ("plan, no ZDR" when the user requires ZDR).

`GET /api/agents/<agent_id>/chats/<chat_id>` includes
`"model": {"provider": "openrouter", "id": "vendor/model", "zdr": true, "require_zdr": false}`
(the override or user default; `zdr` is the effective flag and `require_zdr` the user's
setting). `PUT` to that chat's `/model` endpoint with
`{"model_id": "vendor/model", "zdr": true}` validates the exact ID against the
live catalog with that filter and persists the model, and the flag when it differs
from the user's setting, through the conversations source. Omission of `zdr` (or
null) follows the user's setting; an explicit choice requires the actual boolean,
not a string. Changing models or routing policy is
disabled while the agent has active or queued replies; direct callers receive
HTTP 409. It never interrupts or changes the model of an in-flight reply.
Reopening/switching chats restores their own model and invalidates the cached
context-window size when the model changes.

## reasoning effort per chat

The composer's **Reasoning effort** selector overrides the agent's
`chat_config.reasoning_effort` for the current chat only. Its first option,
**Default**, shows the agent's value and removes the override. The override is
stored with the chat (`<chat_id>.effort.json`), survives restarts, is honored
when the CLI opens that chat, and is deleted with the chat.

The selector is disabled when the agent has `reasoning: false`, or when the
selected model does not advertise `reasoning` or `reasoning_effort` in its
OpenRouter supported parameters (the same check as the Reasoning filter). The
control itself then says why, also on narrow screens: **Disabled in this agent**
(its tooltip points to Agents > Advanced > Reasoning), **Not available in this
model**, or **Unknown for this model** when support could not be verified. Support is cached by the service for ten minutes; if the
catalog cannot be reached, the state is shown as unverified and the value cannot
be changed. Changes are rejected with HTTP 409 while the agent has active or
queued replies.

`GET /api/agents/<agent_id>/chats/<chat_id>/models` includes
`"reasoning_effort": {"override", "default", "effective", "reasoning", "supported"}`
(`supported` is `true`, `false`, or `null` when unverified). `PUT` to
`/api/agents/<agent_id>/chats/<chat_id>/reasoning-effort` with
`{"effort": "high"}` sets the override, and `{"effort": null}` clears it. Accepted
values are `minimal`, `low`, `medium`, `high`, `xhigh` and `max`.

## favorite models

OpenRouter favorites are stored in the ignored
`.config/<user_id>/openrouter-favorites.json`, separately from user YAML:

```json
{"model_ids": ["vendor/model"]}
```

They belong to the user, not an individual agent or browser, and survive
restarts. IDs retain insertion order; adding/removing an existing/missing
favorite is idempotent. Telegram reads this same list every time `/selectmodel`
opens and offers only the favorites present in the current catalog (starred, with
their provider named). Retired or (when ZDR is required) non-ZDR favorites, and those of an unavailable
provider, are not selectable in Telegram. Favorite additions/removals remain web-only; the web
selector can also be used from a mobile browser. The backend provides:

- `GET /api/models/favorites`: saved model IDs.
- `POST /api/models/favorites` with `{"model_id": "vendor/model", "zdr": true}`: add a model
  only if its exact ID is currently in the catalog under that filter (`zdr` defaults
  to the user's `require_zdr`). `zdr: false` permits favoriting a model without ZDR
  routes; it does not change any chat's routing policy.
- `DELETE /api/models/favorites/vendor/model`: remove an ID, even if it is
  no longer available or the user has switched providers.

Each operation resolves the current user before accessing preferences. Favorite
IDs are preferences, not proof of availability: listing them does not contact
OpenRouter, and retired or no-longer-ZDR models stay saved until removed.
Clients must match them against the current catalog before offering selection.
Favorite IDs do not store a routing-policy opt-out and never override the
selector's current filter. Invalid additions return HTTP 400;
catalog failures return 502. Invalid preference files and storage failures
return an explicit 500 rather than silently discarding existing favorites.
Writes use same-directory temporary files and atomic replacement, with updates
serialized within one service instance. Separate server processes do not
coordinate concurrent updates; run one process for preference mutations.
