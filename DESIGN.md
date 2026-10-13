# Anheart dashboard design system

## 1. Atmosphere and identity

An operational dashboard with quiet neutral cards, blue actions, and explicit
machine states. The existing machine detail and live readout cards are the
reference for new health information. The console has its own system in
`raspberry-pi/src/web/DESIGN.md`.

## 2. Color

`app/globals.css` owns the light and dark OKLCH values. Use its semantic
Tailwind tokens: background/foreground, card/card-foreground,
muted/muted-foreground, border, primary/primary-foreground and destructive.
Charts use chart-1 through chart-5. Existing warnings use amber-600 in light
mode and amber-400 in dark mode. Warnings always include words or a glyph;
color alone never communicates the state. No new palette is introduced.

## 3. Typography

Geist Sans and Geist Mono are loaded by the locale layout. Existing scales:
text-3xl bold for page titles, text-lg medium for readings, text-base semibold
for card titles, text-sm for labels and body copy, text-xs for metadata.
Use tabular-nums for measurements and wrap long labels.

## 4. Spacing and layout

Tailwind's 4 px base spacing applies. Cards use p-6, gap-6 between sections,
gap-4 between readings and gap-2 between icon and label. The existing detail
page owns the scroll. New readout grids use one column initially and two or
three at sm/lg, with min-w-0 so contents do not widen the page. History uses
the available card width and h-32, with no nested scroll region.

## 5. Components

- Card: CardHeader with title/description, CardContent for information;
  existing neutral border, rounded-xl and shadow-sm. Loading uses Skeleton;
  empty data is written explicitly. Static cards have no hover animation.
- Badge: default, secondary, destructive and outline variants from ui/badge;
  always readable text, optionally an aria-hidden icon. Health uses secondary
  for unavailable/stale and destructive for a measured threshold violation.
- Readout: semantic dt/dd, muted text-sm label, text-lg tabular value. Unknown
  reads "Unavailable", never zero. Stale is stated next to the last report.
- Health history: temperature and free-disk min/max/last over 24 hours,
  using chart tokens, accessible descriptions and a native details/table
  alternative. Missing readings break the plotted line; never interpolate
  across a missing five-minute bucket.
- Button and Dialog: existing Radix primitives; keyboard focus rings,
  disabled/loading states, escape dismissal and focus restoration retained.

## 6. Motion and interaction

Reuse existing feedback and focus behavior. Readings update directly; no
decorative pulse, counter animation or entrance animation. History is static.
Skeleton animation respects prefers-reduced-motion. Native details provides
keyboard access to numerical history without a bespoke control.

## 7. Depth and surface

Existing mixed strategy: neutral border plus shadow-sm for cards, separate
popover surface for dialogs. Radius base is 0.625rem; sm/md/lg/xl derive from
the global radius variable. State never changes a card's border color.

## 8. Accessibility constraints and accepted debt

Target WCAG 2.2 AA: visible keyboard focus, 4.5:1 body contrast, semantic
headings and readings, explicit stale/unknown states, and non-color warning
labels. History has a numerical alternative. No new accessibility debt is
accepted by this change. This extraction documents existing components; it
does not assert that unrelated dashboard pages have passed an audit.
