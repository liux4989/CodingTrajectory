# Loop Analytics

Operate/read surface for a developer investigating local logs at a desk. Neutral
light surfaces support long-form evidence reading; indigo marks navigable evidence
and focus rather than severity. System sans is the workhorse UI face; monospace is
reserved for IDs and canonical JSON. No illustrative assets ship.

The implementation uses React and freshly installed shadcn/Radix primitives.
`web/src/styles.css` owns semantic color tokens: background `#ffffff`, foreground
`#25272d`, muted `#f4f5f8`, muted foreground `#616777`, primary/ring `#414ac7`,
border `#dfe2e9`, destructive `#b33142`. Body text is 14px with 1.6 line-height;
headings are 28/16/14px. Borders define panes; no decorative shadows or gradients.

Desktop: 65px header, 216px project/saved-view navigation, flexible investigation,
355px exact-evidence pane. At 850px navigation becomes a wrapping row; at 650px
evidence moves ahead of chronology in a single scrolling column. Saved views stay
reachable at narrow widths. All controls keep keyboard focus and visible labels.

The defining interaction is progressive evidence resolution: brief and chronology
first, metadata items next, explicit item content and underlying events last.
Stable links remain separate from saved view identity. Partial brief coverage and
complete selected-item content can coexist and are labeled at their own scopes.
Rendering never implies revision pinning or multi-user authorization.

# Loop Monitor

Monitor adds a second reading mode under the same shell: Strategies (catalog,
permission boundary, watch configuration), watch detail (dry-run preview,
refresh, revision history, per-watch evaluations and findings), Activity (every
evaluation across watches with watch/trigger/state filters), and Findings
(status tabs and lifecycle actions). The same tokens apply; destructive red
(`#b33142`) marks only breaches and errors, while unavailable and pending states
stay neutral outlines so honest "no evidence" never reads as failure or success.

Monitor's defining interaction is explicit condition explanation: every
evaluation leads with one sentence naming the measure, observed value,
threshold, and comparison ("processed_tokens 151,000 exceeds the 50,000 token
budget by 101,000."), with coverage, config revision, and evaluator provenance
one disclosure away. Findings link to the exact turn evidence in Analytics, and
evaluations link to their finding. Dry-run is always visually labeled as a
preview that cannot create findings; refresh is unavailable until the watch is
explicitly enabled.

Verification uses synthetic Amp and Codex evidence exercising pass, breach, and
unavailable turns, desktop Chromium captures of every Monitor surface, and
offline HTTP integration. Design review stays in-thread as explicitly requested.

# Tool mix colors

The Investigation tool mix colors its call sequence by four families, not by
its eight groups: Explore (Read, Search) `#2a78d6`, Change (Edit, Write)
`#eb6834`, Command `#1baf7a`, and Other (Web, Agents, Other) `#c3c6cf`.
Adjacent calls can be any pair, so the palette is validated all-pairs on the
white surface: worst CVD ΔE 9.2, worst normal-vision ΔE 21.5. Only three
categorical hues pass that test; Other is the neutral fold. Command and Other
sit below 3:1 contrast, so the mix table names every group and a legend names
every family. Colors follow the family, never rank; focusing a group dims the
rest instead of repainting it. Tokens live in `styles.css` as `--family-*`.
