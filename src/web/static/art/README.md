# Art slots

The login art of the web UI is **temporary placeholder SVGs**. They are meant to be replaced by pixel/voxel art
(MagicaVoxel renders exported as PNG). Nothing else in the UI has to change: each piece is referenced from **one place**, the custom
properties at the top of `../style.css` (`:root { --art-… }`).

All placeholders use fixed neutral greys (`#8a8a84`, semi-transparent faces) instead of `currentColor`, because they are loaded as CSS
background images, where theme tokens are not available. The element's `opacity` (`--orn-a`, `--orn-arch-a`) keeps them faint on both the
dark and the light theme. A PNG should therefore be drawn in mid-tones with the alpha channel for the background, not baked-in transparency.

| Slot | File | CSS property | Element | CSS size (px) | Export 1x / 2x / 3x |
| --- | --- | --- | --- | --- | --- |
| Login capital | `capital.svg` | `--art-capital` | `.art-capital` | 160 × 92 (112 × 64 when the window is under 800 px tall) | 160×92 / 320×184 / 480×276 |
| Login plinth (the logo stands on it) | `plinth.svg` | `--art-plinth` | `.art-plinth` | 144 × 96; the logo (48 × 42) sits at x 48, y 16, its feet on the top face | 144×96 / 288×192 / 432×288 |
| Login arch | `login-arch.svg` | `--art-login-arch` | `.login-arch` | stretched to the login column (about 344 × 560) | one vector, or a 9-slice image of your choice |

Where they show: on the login page only. Under `forced-colors` all of them are hidden. They are `aria-hidden` and ignore the pointer.

## Swapping a placeholder for a PNG

1. Put the PNG next to the SVG, for example `art/capital.png`, exported at the **3x** size from the table (the browser scales it down).
2. In `style.css` change only the property, for example `--art-capital: url("/static/art/capital.png");`.
3. Set `--art-render: pixelated;` once so the pixels stay crisp when scaled.
4. The plinth keeps its CSS size (`144px 96px`); only the image changes.

Fonts have their own licence file: see `../fonts/LICENSES.md`.
