# obsidian safe/auto overlay

the overlay controls how the agent reads and writes the user's obsidian vault. it exists because a user may not want the agent touching real vault files.

## modes

- **auto**: the agent edits the vault directly. reads and writes hit the real files.
- **safe**: the agent never touches the user's files. it works on a shadow copy and is given the illusion that its edits are real.

the mode is set per bound vault, per agent (see agents.md).

## shadow root (safe mode)

the shadow lives under a reserved folder inside the vault:

```
<vault>/               user's real files
<vault>/ai-agent/      shadow tree, mirrors the vault structure
```

`obsidian:x/y.md` maps to:
- real: `<vault>/x/y.md`
- shadow: `<vault>/ai-agent/x/y.md`

the `ai-agent/` folder is never treated as part of the vault by the agent — it is the mechanism, not content.

## read resolution (safe mode)

1. if a shadow copy exists:
   - if it is a **tombstone** → behave as "not found" (see delete).
   - else → return the shadow copy (the agent's last-known version).
2. else → return the user's real file.

this is stale by design: if the user edits the original after the agent shadowed it, the agent keeps seeing its own version until it reads/writes again.

## operations (safe mode)

- **write**: always writes to the shadow path, creating folders as needed.
- **edit**: resolves content via the read order above, applies the replacement, writes the result to the shadow path.
- **delete**: writes a tombstone to the shadow path (content `deleted`), removes the file from the index and hides it from `list`/`read`/`search`. the user's original is untouched; they can see the intent in `ai-agent/`.
- **move**: moves the shadow copy to the new shadow path and rewrites `[[backlinks]]` inside shadow files. the user's originals are not moved.

in **auto mode** all of the above act directly on the real files (no shadow, no tombstone).

## list merge (safe mode)

`list` returns the union of:
- the user's real files, and
- shadow-only files (created by the agent),

minus any path that is tombstoned. where both exist, the shadow version shadows the original. the `ai-agent/` folder itself is never listed as content.

## tombstones

a tombstone is a shadow file whose content is exactly `deleted`. it is:
- **hidden** from the agent (`list`, `read`, `search`, index),
- **visible** to the user in `ai-agent/`.

## indexing

the index reads obsidian **through** the overlay, so vectors reflect the agent's view: shadow versions replace originals, tombstoned files are excluded.
