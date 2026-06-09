# Context Window UI Redesign

## Reference Design Analysis

Key elements learned from the reference (dogfooding) design:

1. **Header**: Title/subtitle left-aligned, token counter right-aligned in same row
2. **Capacity progress bar**: Full-width bar showing used vs total tokens, with color-coded category segments filling the used portion, empty remainder dark
3. **Legend**: Standalone horizontal row of colored dots + labels, separate from timeline
4. **Compact timeline strip**: Thin bar with per-segment category colors (already exists)
5. **Event list**: Grouped by phase ("BEFORE YOU TYPE ANYTHING", "YOU TYPE IN YOUR TERMINAL"), each item = colored dot + confidence badge + label + token count + per-item progress bar
6. **Detail panel**: Sticky right sidebar with category label, event name, summary text, metadata dl, pin button, and "Key Takeaway" callout

## ASCII Mockup

```
+-------------------------------------------------------------------------+
|  <- Sessions                                                            |
|  Explore the context window         ~19.7K tokens / 200K - illustrative |
|  A simulated session showing what enters context and what it costs      |
|                                                                         |
|  +-------------------------------------------------------------------+  |
|  | ████████|=|=|=|=|=|=|=|=|                                         |  |
|  | ^ green ^ ^ ^ ^ ^ ^ ^ ^                                          |  |
|  |  (used)  | | | | | | | |                                          |  |
|  |     orange | | | | | | |                                          |  |
|  |       blue | | | | | |                                            |  |
|  |        gray| | | | |                                              |  |
|  |       teal | | | |                                                |  |
|  |     orange | | |                                                  |  |
|  |   red-org. | |                                                    |  |
|  |            <-- remaining empty (dark bg) -->                      |  |
|  +-------------------------------------------------------------------+  |
|                                                                         |
|  [square] System  [sq] CLAUDE.md  [sq] Memory  [sq] Skills  [sq] MCP  |
|  [sq] Rules  [sq] You  [sq] Files  [sq] Output  [sq] Claude  [sq] Hks |
|  [eye] = appears in your terminal                                       |
+-------------------------------------------------------------------------+

+-------------------------------------------+  +--------------------------+
|  | BEFORE YOU TYPE ANYTHING               |  |                          |
|  +---------------------------------------+|  |  Hover or click any event|
|  | [dot] auto  System prompt      +4.2K  ||  |                          |
|  |   [████████████████............]      ||  |  Hover to preview. Click |
|  +---------------------------------------+|  |  to pin so you can scroll|
|  | [dot] auto  Auto memory        +680   ||  |                          |
|  |   [████.............]                 ||  | +------------------------+|
|  +---------------------------------------+|  | | KEY TAKEAWAY          ||
|  | [dot] auto  Environment info   +280   ||  | |-----------------------||
|  |   [██...........]                     ||  | | A lot loads before you||
|  +---------------------------------------+|  | | type anything.        ||
|  | [dot] auto  MCP tools (def.)   +120   ||  | | CLAUDE.md, memory,    ||
|  |   [█......]                           ||  | | skills, and MCP tools ||
|  +---------------------------------------+|  | | are all in context    ||
|  | [dot] auto  Skill descriptions +450   ||  | | before your 1st prompt||
|  |   [███............]                   ||  | +------------------------+|
|  +---------------------------------------+|  |                          |
|  | [dot] auto  ~/.claude/CLAUDE.md +320  ||  |  Token impact  1.8K tkns |
|  |   [██.........]                       ||  |  Evidence src  proj file |
|  +---------------------------------------+|  |  Confidence  exact_text  |
|  | [dot] auto  Project CLAUDE.md  +1.8K  ||  |  Terminal    Visible     |
|  |   [██████..........]                  ||  |                          |
|  +---------------------------------------+|  |                          |
|                                           |  |                          |
|  | YOU TYPE IN YOUR TERMINAL              |  |                          |
|  +---------------------------------------+|  |                          |
|  | [dot] auto  Your first prompt    +12  ||  |                          |
|  +---------------------------------------+|  |                          |
+-------------------------------------------+  +--------------------------+
```

## Component Layout Structure

```
<div max-w-[96rem] mx-auto grid gap-5>

  <Card>                             // Header
    <CardHeader>
      <CardTitle>Explore the context window</CardTitle>
      <CardDescription>A session showing what enters context...</CardDescription>
      <CardAction>                   // token counter, right-aligned
        ~19.7K tokens / 200K - illustrative
      </CardAction>
    </CardHeader>
  </Card>

  <CapacityBar />                    // NEW: full-width capacity progress bar

  <LegendRow />                      // Standalone legend (colored dots + labels)

  <div grid-cols-[1.15fr 0.85fr]>   // Two-column layout

    <section>                        // Event stream (left)
      <GroupHeader "BEFORE YOU TYPE ANYTHING" />
      <EventItem /> ...
      <GroupHeader "YOU TYPE IN YOUR TERMINAL" />
      <EventItem /> ...
    </section>

    <aside sticky>                   // Detail panel (right)
      <CategoryLabel />
      <EventTitle />
      <SummaryText />
      <MetadataDL />
      <KeyTakeaway /> (when no event selected)
    </aside>

  </div>

</div>
```

## New Components Needed

### `<CapacityBar>`

Full-width bar showing context window utilization:
- Outer: `rounded-full border border-foreground/14 bg-foreground/7` (h-2 or h-2.5)
- Filled segments: flex children, each with `flexGrow={category.tokens.value}`, colored bg
- Total filled width = `used_percent`% of outer bar
- Remainder = empty/dark background
- Hoverable segments with Radix Tooltip: `"{label}: {tokens} tokens ({pct}%)"`

### `<LegendRow>`

Simple flex-wrap list:
```
<ul flex flex-wrap gap-x-4 gap-y-1.5>
  <li> <span dot /> <span label /> </li>
  ...
  <li> <Eye /> = appears in your terminal </li>
</ul>
```

### `<EventItem>` (refactored from inline)

Each row in the event stream:
```
<button w-full rounded-xl border bg-foreground/5 px-4 py-3>
  <div flex items-center gap-2>
    <span dot />
    <Badge variant="secondary">auto</Badge>
    <span text-muted-foreground>System prompt</span>
  </div>
  <div font-mono text-emerald-400>+4.2K</div>
  <div h-[3px] rounded-full bg-foreground/8>     // per-item progress bar
    <span style={{ width: tokenPercent% }} />
  </div>
</button>
```

## Design Token Audit & Proposal

### Current State: Audit Findings

**Well-structured areas:**
- Font families (`--font-display`, `--font-body`, `--font-mono`) cleanly defined via CSS variables
- Semantic color palette (`--ink`, `--paper`, `--ember`, `--moss`) with proper light/dark pairs
- Category colors now theme-aware via CSS variables
- Radius system (`--radius` + computed sm/md/lg/xl) defined in styles.css

**Inconsistencies found across ALL dashboard web components:**

| Problem | Count | Example |
|---------|-------|---------|
| Arbitrary font-size values | 18 distinct | `text-[0.74rem]`, `text-[0.82rem]`, `text-[0.88rem]`... |
| Hardcoded `rgb(255 255 255 / 8%)` dark border | 8+ in 7 files | `dark:border-[rgb(255_255_255/8%)]` |
| Hardcoded `#eee0bd` / `#2a2620` table head bg | 3 route files | `bg-[#eee0bd] dark:bg-[#2a2620]` |
| `orange-500` Tailwind color used directly | 2 places | `border-orange-500/30`, `bg-orange-500/90` |
| Distinct `border-foreground/N` opacity levels | 7 distinct | `/6`, `/9`, `/11`, `/12`, `/13`, `/14`, `/18` |
| Arbitrary border-radius values | 6 distinct | `[2px]`, `[1.1rem]`, `[1.2rem]`, `[1.4rem]`, `[2rem]` |
| `text-white` hardcoded | 4 places | Destructive button, badge, dialog |
| `font-[850]` one-off weight | 1 place | metric-card.tsx |
| Identical large shadow copy-pasted | 3 places | `0 24px 70px rgb(49 42 25 / 18%)` |

### Proposed: 4-Tier Token System

Following the [modern-web-guidance CSS architecture guide](https://developer.mozilla.org/en-US/docs/Web/CSS), tokens are organized in tiers with each tier building on the previous one.

#### Tier 1: Literal Tokens (raw palette, already defined)

These are the raw color values. Already exist in `:root` and `[data-theme="dark"]`. No changes needed.

```
--ink, --paper, --paper-strong, --line, --accent-teal, --ember, --moss, etc.
--category-system, --category-memory, --category-skills, etc.
```

#### Tier 2: Semantic Tokens (meaning-based, mostly exist)

Already defined:
```css
--foreground: var(--ink);
--background: var(--paper);
--card: var(--paper-strong);
--primary: var(--accent-teal);
--destructive: var(--ember);
--muted-foreground: var(--muted-color-text);
```

**NEW tokens to add:**

```css
/* Surface tiers for layered backgrounds */
--surface-0: var(--paper);           /* page background */
--surface-1: var(--paper-strong);    /* cards, panels */
--surface-2: var(--ink) / 5%;       /* subtle raised areas (light) */

/* Border tiers */
--border-default: var(--border);     /* primary borders */
--border-subtle: var(--ink) / 8%;   /* dividers, separators */
--border-strong: var(--ink) / 18%;  /* emphasis borders */

/* Status colors */
--success: var(--moss);
--warning: #b45309;                  /* light */ / #d97706 (dark) */
--info: var(--accent-teal);

/* Overlay */
--overlay: var(--ink) / 30%;
```

#### Tier 3: UI Tokens (purpose-specific)

**Typography scale** -- consolidate 18 arbitrary sizes into 6 semantic roles:

```css
/* Type scale (rem-based, honors user font-size preference) */
--text-eyebrow: 0.75rem;    /* 12px - uppercase kickers, "BEFORE YOU TYPE..." */
--text-caption: 0.8125rem;  /* 13px - labels, tooltips, metadata */
--text-body-sm: 0.875rem;   /* 14px - body secondary, timestamps */
--text-body: 1rem;          /* 16px - primary body, event labels */
--text-heading: 1.5rem;     /* 24px - section headings, detail panel */
--text-display: clamp(2rem, 4vw, 3.5rem);  /* page titles */
--text-metric: clamp(2rem, 4vw, 3.8rem);   /* metric card numbers */

/* Tracking (letter-spacing) scale */
--tracking-tight: -0.03em;  /* display headings */
--tracking-normal: 0;       /* body text */
--tracking-wide: 0.08em;    /* uppercase labels */
--tracking-wider: 0.14em;   /* eyebrow kickers */

/* Leading (line-height) scale */
--leading-tight: 0.95;      /* display headings */
--leading-snug: 1.25;       /* sub-headings */
--leading-normal: 1.5;      /* body text */
--leading-relaxed: 1.625;   /* long-form summaries */
```

**Shadow scale:**

```css
--shadow-sm: 0 1px 2px var(--ink / 5%);
--shadow-md: 0 4px 12px var(--ink / 8%);
--shadow-lg: 0 14px 32px var(--ink / 12%);
--shadow-popover: 0 24px 70px var(--ink / 18%);  /* tooltips, dialogs */
```

**Surface opacity scale (for bg-foreground/N patterns):**

```css
--surface-hover: var(--ink) / 8%;
--surface-active: var(--ink) / 12%;
--surface-track: var(--ink) / 7%;   /* progress bar tracks */
--surface-fill: var(--ink) / 5%;    /* event item backgrounds */
--surface-divider: var(--ink) / 9%; /* DL row separators */
```

**Border radius consolidation** -- use existing `--radius` system:

```css
/* Already defined, just enforce usage: */
--radius-sm: calc(var(--radius) - 4px);  /* 8px - badges */
--radius-md: calc(var(--radius) - 2px);  /* 10px - inputs, inner */
--radius-lg: var(--radius);               /* 12px - cards */
--radius-xl: calc(var(--radius) + 4px);  /* 16px - event items */
--radius-2xl: calc(var(--radius) + 8px); /* 20px - tables */
--radius-3xl: calc(var(--radius) + 20px);/* 32px - route headers */
--radius-full: 9999px;                   /* pills, circles */
```

#### Tier 4: Component Tokens (future, not needed now)

Only add when a component needs to override Tier 3 tokens. Examples:
```css
--button-bg-primary: var(--primary);
--table-head-bg: var(--surface-2);
```

### Migration Plan

**Phase 1: Add Tier 2+3 tokens to styles.css**
- Add semantic border tokens (`--border-subtle`, `--border-default`, `--border-strong`)
- Add type scale tokens (`--text-eyebrow` through `--text-display`)
- Add shadow scale tokens (`--shadow-sm` through `--shadow-popover`)
- Add surface opacity tokens (`--surface-hover`, `--surface-fill`, etc.)
- Add `--warning` color with light/dark variants
- Add `--radius-2xl` and `--radius-3xl`

**Phase 2: Migrate components to use tokens**
- Replace all `dark:border-[rgb(255_255_255/8%)]` with `dark:border-border-subtle`
- Replace all `bg-[#eee0bd] dark:bg-[#2a2620]` with `bg-table-head` (new semantic)
- Replace arbitrary font sizes: `text-[0.74rem]` → `text-eyebrow`, `text-[0.82rem]` → `text-caption`, etc.
- Replace `orange-500` with `text-warning` / `border-warning`
- Replace copy-pasted shadows with `shadow-popover`
- Replace `font-[850]` with `font-extrabold` (nearest standard weight)

**Phase 3: Add to Tailwind theme**
Map all new CSS variables in `@theme inline` so they work as Tailwind utilities:
```css
@theme inline {
  --text-eyebrow: var(--text-eyebrow);
  --text-caption: var(--text-caption);
  /* ... enables text-eyebrow, text-caption utilities */
}
```

### Tailwind v4 Integration

In Tailwind CSS v4, the `@theme inline` block maps CSS custom properties to Tailwind utilities. The approach:

1. **Define raw values in `:root` / `[data-theme="dark"]`** (Tier 1 + 2)
2. **Reference them in `@theme inline`** via `var()` so Tailwind generates utilities
3. **Components use Tailwind utilities classes** (e.g., `text-eyebrow`, `border-border-subtle`)
4. **Never use arbitrary values** (`text-[0.74rem]`) in components -- always go through tokens

This ensures:
- Single source of truth for all design values
- Light/dark mode works automatically
- Consistent usage across all components
- Easy to tune globally by changing one variable

