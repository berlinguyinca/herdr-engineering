# Reading and sharing HerdR artifact reports

How a report share looks to a reader, and what the owner can switch on in the share panel. The page is produced by
`herdr_engineering/control_plane/artifact_report_render.py` (the same renderer serves the public share viewer and the
dashboard preview). For the share flow itself see [share-links.md](share-links.md).

## Rich view (opt-in, share panel)

A report share normally holds only the generated preview page. In the share panel's **Links** tab the form shows an
**Include the author's HTML page (Rich view)** checkbox, only when the audience is **public** and the chosen version has
exactly one HTML asset. It is off by default. The approval summary then lists the author's page and marks it.

When it is on:

- The author's page is copied into the share as `author-page.html` and the preview gains a **Rich view** section with a
  **Show rich view** / **Hide rich view** button (the section is hidden until its small inline script reveals it).
- The page is framed in an iframe with an empty `sandbox` and served under a no-script CSP, so it is shown **without
  scripts**; the view says "Scripts are disabled in this view". Anyone with the link can see it.
- The sanitised preview stays the default and is unchanged until the button is pressed. Print never includes the
  Rich view section.

## Linking to a finding (anchors)

Each section, finding and figure heading carries a visible `#` link (24px target, keyboard focusable, named
"Link to this finding" / "section" / "figure"). Copy it to get a URL ending in the fragment. The fragments are:

| Target | Fragment |
| --- | --- |
| Section | `#sec-<name>` (`questions`, `findings`, `figures`, `evidence`, `downloads`, `analysis`, `limitations`, `next-checks`) |
| Finding | `#finding-<id>` |
| Figure | `#figure-<id>` |
| Evidence row | `#evidence-<id>` |
| Question | `#question-<id>` |

`<id>` is the descriptor's own id. The Question, Evidence and Figure tags under a finding jump to those entries. The
linked target is outlined and highlighted. Permalinks and the legend are emitted only when the preview size cap is at
least 16 KiB (the default is 256 KiB); a smaller cap renders without them.

## Printing and saving as PDF

Use the browser's Print, then "Save as PDF". The print stylesheet:

- forces a light, black-on-white palette whatever the screen theme;
- hides the contents pills, buttons, `#` links, the legend and the Rich view section;
- keeps each finding, figure, callout and table row on one page, and keeps headings with what follows;
- keeps the **real tables** (no stacked blocks), with the header row repeating on each page and no sideways scroll box;
- prints the folded Analysis block open, shrinks a tall figure to fit one page, and writes the destination after
  external links.

## "How to read this report" legend

A collapsed section under the title (screen only, hidden in print). It explains only what the report actually shows:
Finding, the Question/Evidence/Figure tags, Limits, Next checks, a figure's "How to read" note, Evidence and
downloads (including the first 12 characters of each file's SHA-256), Method and configuration, `#` links, and the
"Shortened or omitted" notice of a size-limited preview. It states that severity and confidence are not used: findings
are not ranked or scored.

## Phone width

Below 600px (screen only) each row of the **Evidence** and **Separate downloads** tables becomes a labelled block
(Evidence / Description / Files; Name / Role / Type / Size / SHA-256), so nothing is cut off and the table does not
scroll sideways. The header row stays for assistive technology, and the tables carry explicit table roles because
browsers drop table semantics once the layout changes. `#evidence-<id>` links still work. Desktop and print keep the
real table.

## Keyboard use of the share panel tabs

The panel has four tabs: **Links**, **Collaborators**, **Activity**, **Feedback**. Tab moves focus to the tab row once
(only the selected tab is a tab stop); then:

| Key | Effect |
| --- | --- |
| Right arrow | next tab, wrapping from Feedback to Links |
| Left arrow | previous tab, wrapping from Links to Feedback |
| Home / End | first / last tab |

Moving selects the tab and shows its panel. Combinations with Alt, Ctrl or Meta are ignored.

## Tests

`tests/playwright/report-rich-view`, `report-anchors`, `report-print`, `report-how-to-read` and `report-narrow-tables`
(`BROWSERS` selects engines; default `chromium,firefox`) and `packages/artifact-share-panel.test.js`.
