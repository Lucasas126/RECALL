---
description: "Use when creating or changing RECALL project documentation, including README.md and the architecture, configuration, source, agent, and tool guides."
applyTo: "docs/**/*.md,README.md"
---
# Documentation guidelines

- Write concise, practical English, matching the existing project documentation. Use lowercase headings where that is already the convention in the surrounding document.
- Explain the behavior developers can verify in code. Clearly separate implemented behavior from planned design, and keep status notes current.
- Prefer updating the existing canonical page over adding a duplicate guide. Link related topics such as architecture, configuration, sources, agents, overlays, and tools rather than copying their content.
- Use relative Markdown links to existing project files, and verify that their targets and any section anchors exist.
- Keep examples consistent with the current schema, source names, paths, and CLI. Never include real credentials, private user data, or sensitive log excerpts.
- Update documentation alongside changes to externally visible behavior, configuration, setup, or developer workflows.
