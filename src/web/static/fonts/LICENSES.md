# Bundled fonts

RECALL serves these fonts itself (no CDN, no request to any third party). All three are licensed under the
**SIL Open Font License 1.1**; the licence text is in [OFL.txt](OFL.txt) and must travel with the font files.

| File | Family | Used for | Upstream |
| --- | --- | --- | --- |
| `fraunces.woff2` | Fraunces (variable: wght, opsz, SOFT, WONK; upright only) | wordmark, login title, empty-state headline | https://github.com/undercasetype/Fraunces |
| `inter.woff2` | Inter (variable: wght, opsz; upright only) | all interface and chat text | https://github.com/rsms/inter |
| `geist-mono.woff2` | Geist Mono (variable: wght) | code, tool arguments, version line | https://github.com/vercel/geist-font |

The files are the unmodified upstream WOFF2 builds, renamed. Replacing one means keeping its `@font-face` rule in `style.css` in step.
