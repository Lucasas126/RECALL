# plan: seamless restarts (a deploy should not break a session)

Status: **plan, not built.** Each step starts with a spike that proves the mechanism; the design below is revised
with what the spikes show, then this file becomes the final design note.

## goal

A release restarts the server (Watchtower recreates the container). Today that:

- cuts a running reply: the last tool-round checkpoint stays as an `interrupted` turn and the chat shows
  "[Response cut off...]"; the model sees the cut and the user has to ask it to go on;
- kills every background process (`process_start`): the local ones die with the container, the ssh ones lose
  their local `ssh` client; `recover` marks them `lost` and nobody is woken, so an agent waiting for one never
  hears about it;
- drops a message sent in the restart window and ends subagent tasks as `cancelled`.

Target: the user sees a short "updating, reconnecting" notice and nothing else. A reply goes on from what was
saved, with no note to the model. A background process keeps running, or its loss is reported to the agent.

## what exists (0.3.20)

- `Agent._checkpoint()` saves the running reply (text, reasoning, actions, timeline) **after each tool round**,
  as an `interrupted` turn with `checkpoint: true` (the chat's last line, replaced by the next checkpoint and by
  the final turn). Text streamed after the last tool round is not saved.
- `_load_recent_context()` rebuilds a stored turn's tool rounds (`_restore_tool_rounds`), adds
  `_CHECKPOINT_MARKER` to a checkpoint left behind.
- The associative-memory pause already continues a reply mid-text: `self._partial` is sent as a trailing
  assistant message plus `_HIT_CUT_NOTE` ("continue from exactly where it stopped") and the texts are joined with
  `_continuation_gap`. Resuming after a restart reuses this path.
- `ChatService` keeps the running reply, its subscribers and the approvals in memory; `close()` cancels
  subagent tasks and denies pending approvals.
- `ProcessRegistry`: one JSON record + log per process under `<data>/<user>/.processes/`. Local processes are
  `Popen` children; ssh ones run `setsid -w bash -c 'echo $$ > /tmp/recall-proc-<id>.pid; <cmd>; ...'` and the
  output comes back through the local ssh client. `recover()` marks running records `lost`.
- The web page follows a running reply with `followReply` (replay + live), retrying up to 5 times when the chat
  is still active.
- uvicorn runs with defaults (`web.py`); compose has no `stop_grace_period` (Docker's default is 10 s).

## design

### 1. run journal (base for everything)

- When a reply starts, write `<chat dir>/.runs/<chat_id>.json`: user turn already persisted (yes/no), channel,
  model, started, attempt count, `generation_id`, the tool call in flight (name, arguments, call id, started) or
  none. Removed when the turn ends normally or by the user's interrupt.
- The user turn is written to the chat file before the reply starts (verify the current order; fix it if not),
  so a message sent just before the restart is never lost.
- Checkpoint more often: also on a timer (every ~3 s of new text) while text streams, not only after tool
  rounds, so the saved partial is at most a few seconds behind. Cost: one rewrite of the chat's last line.

### 2. resume a cut reply (no note to the model)

At start-up, after the services are up, for each run journal left behind:

- Load the chat; the checkpoint becomes the agent's `_partial` (text) and restored tool rounds, exactly as the
  associative pause does, but **without** `_CHECKPOINT_MARKER` and with a neutral system line instead of
  `_HIT_CUT_NOTE` ("Continue your reply from exactly where it stops, without repeating anything").
- Run the reply again under the same chat slot; subscribers that reconnect see the replay plus the new text.
  Keep the same `generation_id` if the page can join it (spike), otherwise the page reloads the chat once.
- A tool call in flight at the cut (journal has `tool in flight`): **the model is told**, by a tool result for
  that call: "This call was cut off by a server restart before its result came back. It may or may not have
  run, fully or partly. Check its effect before doing anything else." The UI shows the same on the action card.
  No tool is re-run automatically.
- Attempts are capped (2): a crash loop must not resume the same reply forever; after the cap the turn is left
  as today (cut off, marker shown).
- Providers: the continuation sends a trailing assistant message. Spike each one (OpenRouter with an Anthropic,
  an OpenAI and an open model; Claude subscription; Ollama). If one rejects a trailing assistant message or
  continues badly, fall back to the user-role note the associative pause uses.

### 3. background processes

- **ssh backends** (`pc`, `laptop`, `orion`, `dev`...): run the command fully detached on the target:
  `setsid nohup bash -c '<cmd>' > /tmp/recall-proc-<id>.log 2>&1 < /dev/null; echo $? > .../<id>.code`, with the
  pid file as today. RECALL follows it by polling over ssh (tail of the log, presence of the code file) instead
  of holding a stream open. After a restart the poller simply goes on from the record. Log size capped on the
  target (rotate or truncate as the local pump does).
- **local backend** (the RECALL container itself): cannot survive. At start-up, `recover()` marks it `lost`
  (as today) **and wakes the chat** that started it, with: "Process <id> (<command>) was killed by a server
  update after <t>. Its last output is below. Check how far it got and start it again if it is still needed."
  Same for local processes of the sandbox backend if it is a local one.
- Any process that **ended while the server was down** (code file found at the first poll) wakes its chat as a
  normal end does today.
- Workers (subagent tasks): out of scope for the first version; they keep ending as `cancelled` with their
  partial work, and the parent agent is woken with that (check it is today).

### 4. orderly stop and notice

- `docker-compose.yml`: `stop_grace_period: 60s` for `recall-web` and `recall-telegram`; uvicorn
  `timeout_graceful_shutdown`.
- On SIGTERM: stop accepting new replies (they get "updating, retry in a moment", the page retries), send a
  `notice` event to every open stream, write every run journal and a final checkpoint, wait up to N seconds
  for replies that end on their own, then exit. Replies still running are resumed by the new process.
- The page shows "Updating RECALL, reconnecting..." on that notice or on a dropped stream, retries with backoff
  for up to ~2 min (today `followReply` stops after 5 attempts) and then follows the resumed reply.
- Telegram: a reply cut by the restart is resumed by the bot process the same way; the user sees the message
  being edited again.

## limits (accepted, to be documented in the user guide)

- A gap of seconds while the container is replaced; the page shows "reconnecting".
- A reply resumes from its last checkpoint: up to ~3 s of streamed text may be generated again (it is not shown
  twice: the replay ends at the checkpoint). Reasoning in progress is lost and redone.
- A tool cut mid-call is never re-run; the model is told which one and to check its effect.
- Processes on the local backend die; the agent is told and restarts them if needed.
- Approvals pending at the restart are asked again (the card reappears).
- A crash (kill -9, OOM) skips the orderly stop: only the resume from disk applies.
- Two restarts in a row: the resume is capped at 2 attempts per reply.
- Subagent tasks are not resumed (first version).
- A data format change between two versions could break the resume; releases keep the run journal and the
  checkpoint format compatible with the previous version.

## spikes before building (in order)

1. **Detached ssh process.** On `dev` (and a second machine): start with `setsid nohup`, kill the local ssh
   client, check the remote process survives, writes its log and its code file; check the pid file and
   `kill_remote` still work. Also on Windows Git Bash (`pc`): `setsid` may not exist there; find the
   equivalent or document that `pc` processes do not survive.
2. **Provider continuation.** Same short prompt, cut a reply at a fixed point, send it back as a trailing
   assistant message: OpenRouter (Claude, GPT, an open model), Claude subscription, Ollama. Record: accepted?
   continues mid-word without repeating? tool calls still work after it?
3. **Restart in the dev stack.** Run the web app from `dev`, start a long reply with tools and a background
   process, `kill -TERM` the server, start it again: what the chat file and `.processes/` hold, what the page
   does. This is the baseline the tests will reproduce.
4. **Page reconnect.** With the same setup, check whether `followReply` can join the resumed run (same
   `generation_id`) or needs a reload.

## spike results

### spike 1: detached ssh process (2026-11-14, `dev`)

- `setsid nohup ... &` with the parent killed by SIGKILL: the child keeps running and writes its output and its
  code file. Confirmed.
- Today's ssh process (`setsid -w bash -c ...` through the container's `ssh` client), local `ssh` killed with
  SIGKILL: a command that **does not print after the cut** survives and finishes (wrote its file 20 s later; the
  pid file was removed at its end). A command that **keeps printing** dies at its next write (SIGPIPE on the
  dead ssh channel): a 1 s ticker stopped at tick 5 of 20. So today processes survive only by luck.
- Fix confirmed by the first test: redirect stdout/stderr to a log file on the target and detach stdin
  (`> /tmp/recall-proc-<id>.log 2>&1 < /dev/null`), write the exit code to a file, follow by polling.
- The record keeps state `exited, -9` when the local ssh dies even though the remote job goes on: the state must
  come from the code file on the target, not from the local client.
- Windows `pc` (Git Bash, MSYS 3.6.7): **no `setsid`**, only `nohup` and `disown`. Today's `process_argv`
  (`setsid -w bash -c ...`) therefore cannot work there at all. `nohup bash -c '...' > log 2>&1 < /dev/null &
  disown` survived the end of its ssh session and finished (8/8 ticks, code file written). Open: how to kill the
  whole tree without a process group (`kill -- -pgid` under MSYS, or `taskkill /T /PID` on the Windows pid);
  pick the launcher per backend (`setsid` when present, else `nohup` + `disown`).

## steps (after the spikes)

1. Run journal + user turn first + timed checkpoints; tests.
2. Resume at start-up (text only, no tool in flight); tests with a fake provider that records the context.
3. Tool in flight: the cut-off tool result and the action card; tests.
4. Detached ssh processes with polling; recover/wake for local ones; tests with a local ssh to `dev`.
5. SIGTERM handling, notice event, page reconnect with backoff, compose grace period.
6. User guide: what survives a restart and what does not (the limits above).

## not in this plan

- Zero-downtime deploys (two containers behind a proxy, `docker rollout`): see the research note in the
  the assistant's palace; possible later, needs per-chat locks on disk instead of the in-memory slot.
- Resuming subagent tasks.
