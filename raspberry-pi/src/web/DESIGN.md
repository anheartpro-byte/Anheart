# Local operator console design contract

Extracted from the existing offline panel in `static/index.html`, `static/app.css`
and `static/app.js`. ANH-71 restores an existing state; it does not redesign this
interface or introduce new styling.

## 1. Atmosphere and identity

Dark, dense, offline operator console. Measured state, commands and draft
commands remain distinct. The fixed emergency controls must remain reachable.

## 2. Color

| Existing token | Value | Role |
| --- | --- | --- |
| `--bg` | `#14161a` | Page |
| `--panel` | `#1d2026` | Cards and sidebar |
| `--panel-2` | `#23272f` | Raised/hover surface |
| `--line` | `#32373f` | Separation |
| `--ink` | `#e8eaee` | Primary text |
| `--ink-dim` | `#9aa2ae` | Secondary text |
| `--ink-faint` | `#6b7280` | Metadata |
| `--bad` | `#e5484d` | Stop, fault, latched verdict |
| `--warn` | `#f5a524` | Warning, stale or unconfirmed state |
| `--good` | `#30a46c` | Measured standstill/in-zone state |
| `--accent` | `#6ea8fe` | Existing navigation/focus affordances |

## 3. Typography

Body: `15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
Numeric values use `--mono`: `ui-monospace, "DejaVu Sans Mono", "Courier New",
monospace`. Existing card titles use `0.82rem`; pills use `0.72rem`.

## 4. Spacing and layout

Preserve the existing flex shell and wrapping card rows (basis `22rem`, gap
`1rem`). Sidebar width is `15.5rem`; fixed footer height is `--footer-h: 5.5rem`.
The sidebar owns its scroll. Existing responsive transitions are at `900px`
and `560px`. This extraction does not rename the existing raw spacing values.

## 5. Components and states

- Cards: `.card`, existing `10px` radius and panel background.
- Pills: `.pill` with semantic good/warn/bad variants and textual state.
- Buttons: `.btn`, existing hover/disabled behavior; stop and emergency stop
  remain separate controls in the fixed footer.
- Readouts: `.grid` description/value pairs with monospaced numeric values.
- Visibility: `.hidden` is `display: none !important`; `show()` owns this state.
- Manual card: `#manual-idle` contains the named operator and start controls;
  `#manual-controls` contains the draft/target controls. A snapshot in `repos`
  returns to idle even when its last manual-session record remains available.
  A manual ramp in `arret` remains displayed until the machine returns to rest.
  The `rampe` line has three wordings: a ramp in progress with its arrival
  time, `cible atteinte` when the setpoint of the snapshot is the target, and
  `consigne maintenue, cible non atteinte` when a `freeze` holds the setpoint
  away from a non-zero target (`ramping` false with target and setpoint
  different). The ramp banner follows `ramping` alone, so it is not shown over
  a held setpoint.

## 6. Motion and interaction

Existing sidebar transform transition: `0.18s ease-out`. Disconnection banner
uses a brightness pulse; reduced motion uses a solid banner. Draft +/- edits
do not send commands; only Apply submits a target. No new motion is introduced.

## 7. Depth and surface

Preserve existing panel tones and neutral separators. Existing radii are `4px`,
`6px`, `8px`, `10px` and pill radii. Banner shadow remains part of the existing
disconnection signal. No new elevation or accent treatment is introduced.

## 8. Accessibility constraints and debt

Preserve native buttons/inputs, labels, textual safety status, reduced-motion
behavior and visible emergency controls. ANH-71's browser checks cover manual
card states and restart controls; no Lighthouse or full WCAG certification is
claimed. Raw spacing, small metadata text and existing accent-bordered states
are observed pre-existing design debt, not newly accepted waivers or a mandate
to restyle safety controls in this bug fix.
