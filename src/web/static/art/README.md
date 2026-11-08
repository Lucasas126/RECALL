# Art slots

The architecture ornaments of the web UI are **temporary placeholder SVGs**. They are meant to be replaced by pixel/voxel art
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
| Chat column (two, flanking the chat) | `column.svg` | `--art-column`, `--art-column-slice` | `.art-column` | 40 wide × the chat height | one image 40×66: capital 40×28, shaft 40×16, base 40×22 |
| Header arcade | `arcade.svg` | `--art-arcade` | `.art-arcade` | tile 12 × 8, repeated along the header line | 12×8 / 24×16 / 36×24 |

Where they show: the login art always; the columns only on windows at least 1100 px wide whose chat area is at least 880 px (so they never
touch the text); the arcade on every size. The **Architecture ornaments** switch in Settings > General turns columns and arcade off
(`data-ornaments="off"` on `<html>`). Under `forced-colors` all of them are hidden. They are `aria-hidden` and ignore the pointer.

## Swapping a placeholder for a PNG

1. Put the PNG next to the SVG, for example `art/capital.png`, exported at the **3x** size from the table (the browser scales it down).
2. In `style.css` change only the property, for example `--art-capital: url("/static/art/capital.png");`.
3. Set `--art-render: pixelated;` once so the pixels stay crisp when scaled.
4. `column.svg` is cut into three bands with `border-image`: capital on top, a shaft band that stretches vertically, base at the bottom. The
   slice is given **in image pixels**, so for a PNG that is 3x tall, set `--art-column-slice: 84 0 66 0` (28, 22 × 3). Keep the shaft
   band uniform from top to bottom, because it is stretched.
5. The arcade tile and the plinth keep their CSS sizes (`12px 8px`, `144px 96px`); only the image changes.

Fonts have their own licence file: see `../fonts/LICENSES.md`.
