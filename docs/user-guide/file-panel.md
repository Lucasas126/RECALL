# File panel

The file panel lets you browse and read the files an agent can reach, without asking the agent to paste them: its sources
(mind palace, uploads, your folders) and, when it has the terminal group, its terminal backends.

- Pick a root, open folders, and view Markdown, text, code, images, PDFs (page by page), CSV and HTML (shown sandboxed). Other files can be downloaded.
- It is read-only: nothing can be edited, deleted or run from the panel.
- Hidden entries (names starting with `.`) and secrets (keys, `.env` files, the secrets file) are never listed or served.
- Backends listed in `file_panel.open_backends` (default `local` and `dev`) open at once. Any other backend asks you to allow it first; the permission lasts 8 hours or until you log out.
- Files over 25 MiB are not served; folders with more than 2000 entries are cut. Both limits are in [settings.yaml](../reference/settings-yaml.md#file_panel).
- Every read on a backend is written to the agent's terminal audit trail.

Developers: [file panel API](../architecture/file-panel-api.md).
