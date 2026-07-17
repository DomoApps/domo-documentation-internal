---
name: csv-to-mdx
user-invocable: true
description: "review converted MDX article, audit programmatic conversion output, validate Salesforce article conversion, post-conversion review"
argument-hint: "path to an MDX file or article title to review"
---

Review and audit an MDX article produced by `scripts/migrate_internal_docs.py`. Identify anything the script cannot fix automatically and provide a corrected version.

The user will provide: $ARGUMENTS

---

## Pipeline Overview

The full programmatic conversion runs in this order:

1. **CSV filtering** — `migrate_internal_docs.py` reads `Internal_Docs.csv`, filters rows where `Destination` starts with "Mintlify" and URL is on `domo-support.domo.com`.
2. **Salesforce fetch** — the article HTML body is fetched from the Salesforce Knowledge API using the URL slug and a session token.
3. **Pre-processing** — `_strip_toc_elements()` removes ToC jump-link lists, "Back/Return to top" navigation, and orphaned `---` horizontal-rule artifacts before the HTML is converted.
4. **markdownify conversion** — `_DomoMDXConverter` converts HTML to Markdown with image placeholders, rewriting internal Salesforce URLs to repo-relative `/s/article/...` paths.
5. **Image restoration** — `_restore_images()` replaces placeholders with either `<Frame>` screenshot blocks or inline `<img>` icon syntax, based on image dimensions and HTML context.
6. **Callout conversion** — `_convert_callouts()` converts `**Note:**`, `**Important:**`, `**Warning:**`, `**Tip:**` paragraphs to `<Note>`, `<Warning>`, `<Tip>` components.
7. **FAQ conversion** — `_convert_faq()` converts FAQ sections to `<AccordionGroup>`/`<Accordion>` components.
8. **Artifact cleanup** — strips `— | —` Salesforce divider artifacts, collapses consecutive `---` lines, removes leading `---` artifacts, and normalizes excess blank lines.
9. **Frontmatter** — wraps the result in a `---\ntitle: "..."\n---` YAML block.

---

## Formatting Rules (from New-Article-Template.mdx)

Apply these exact MDX patterns wherever the corresponding element appears.

**Screenshots**
```mdx
<Frame>![alt text describing the screenshot](/images/kb/image-name.png)</Frame>
```
- Wrapping in `<Frame>` is required — it auto-sizes the image to the content column width.
- Alt text must describe what the screenshot shows, not just "screenshot."

**Inline icons**
```mdx
<img alt="alt text" src="/images/kb/icon-name.png" style={{width: 20, height: 20, display: 'inline', verticalAlign: 'start', margin: '0'}}/>
```
- Use for small icons embedded mid-sentence (≤ 40px in either dimension).

**Callout components** (always bold the label inside; always a blank line before the callout in body text)
```mdx
<Note>**Note:** Text here.</Note>
<Warning>**Important:** Text here.</Warning>
<Tip>**Tip:** Text here.</Tip>
```

**Description lists**
```mdx
- **Term —** description of term
- **Another Term —** description of another term
```

**FAQ section**
```mdx
## FAQ

<AccordionGroup>

<Accordion title="Question text here?">
Answer text here.
</Accordion>

</AccordionGroup>
```

**Frontmatter** — every article needs at minimum:
```mdx
---
title: "Article Title Here"
excerpt: "Single sentence summarizing what the article covers."
---
```

**Tables** — pipe tables must be padded with spaces so columns align vertically:
```mdx
| First Column | Second Column |
| ------------ | ------------- |
| Row text 1   | Row text 1    |
| Row text 2   | Row text 2    |
```

---

## Article Structure (from Domo-KB-Style-Guide.mdx)

Required section order:
1. **Intro** — followed immediately by a `---` horizontal rule
2. **Required Grants**
3. *(Optional)* **Prerequisites**
4. **Access [Feature Name]** (can swap with Required Grants if the grant is needed to see the access path)
5. **Task headings** in CRUD order — Create, Review, Update, Delete (include only those that apply)
6. *(Optional)* **FAQ**
7. *(Optional)* **Troubleshoot**
8. *(Optional)* **Related Articles**

**Intro section format:** One sentence in most cases. Use "This article explains how to…" or "This article covers…" followed by 2–3 concrete actions or skills the reader gains. Do not explain why the skills matter.

**Heading Hierarchy:** The frontmatter `title` renders as H1. Top-level sections (Intro, Required Grants, tasks, FAQ, etc.) are H2. Subsections are H3+.

---

## Style Rules

### A — Enforced programmatically by the migration script

These are already handled and should be correct in any script output:

| Rule | What the script does |
|------|---------------------|
| No table of contents | `_strip_toc_elements()` removes jump-link `<ul>`/`<ol>` lists |
| No "Back/Return to top" links | `_strip_toc_elements()` removes these |
| No `— \| —` divider artifacts | Stripped with regex |
| No consecutive `---` artifacts | Collapsed to single `---`; leading `---` removed |
| Callout blocks use MDX components | `_convert_callouts()` converts bold-label paragraphs |
| FAQs use AccordionGroup | `_convert_faq()` converts FAQ sections |
| Internal links are repo-relative | `convert_a()` rewrites `domo-support.domo.com/s/article/…` links |
| Screenshots wrapped in `<Frame>` | `_restore_images()` wraps large/standalone images |
| Inline icons use `<img style>` | `_restore_images()` uses dimension + context signals |
| YAML frontmatter added | `html_to_mdx()` wraps output in `---\ntitle: "..."\n---` |

### B — Requiring human review after conversion

The script cannot reliably fix these. Review every converted article for:

**Frontmatter**
- [ ] Add `excerpt` field — a single sentence summarizing what the article covers (required by style guide)

**Article structure**
- [ ] Intro → `---` → Required Grants → (Optional: Prerequisites) → Access Feature → CRUD tasks → (Optional: FAQ) → (Optional: Troubleshoot) → (Optional: Related Articles)
- [ ] Intro section uses "This article explains how to…" or "This article covers…" format
- [ ] Any `<!-- TODO: embed image → ... -->` comments must be resolved manually (image download failed — re-run with a valid token or download the image manually)

**Image paths — case collisions**

Salesforce image IDs are case-sensitive, but macOS APFS is case-insensitive. Two filenames that differ only by case collapse to one file on disk, silently breaking articles.

- [ ] For every new image under `images/kb/`, check whether its lowercase form matches an already-tracked path: `git ls-files images/kb/ | tr A-Z a-z | sort | uniq -d`. If it collides, rename the new file by inserting `-2` before the extension and update every reference in the article.

**Inline icons (image → font)**

The converter emits image-based inline icons (`<img src="/images/kb/...">`). Any inline icon depicting a Domo UI element should be migrated to the font convention. See `Domo-KB-Style-Guide.mdx` › **Icons** for full guidance.

- [ ] `<img>` icons for **current Domo product UI** → migrate to `<i className="icon-{name}" aria-hidden="true" />` (Phosphor)
- [ ] `<img>` icons for **legacy surfaces** (Workbench, pre-refresh UI) → migrate to `<i className="legacy-icon-{name}" aria-hidden="true" />`
- [ ] `<img>` icons for **third-party brand logos** → use `<Icon icon="{slug}" iconType="brands" />` (Font Awesome) or inline SVG with `fill="currentColor"` (Simple Icons)
- [ ] After migration, confirm the surrounding prose names the icon

**Headings**
- [ ] All headings use the **imperative mood** — never the gerund. **Correct:** "Connect a DataSet" **Incorrect:** "Connecting a DataSet"
- [ ] Top-level sections are H2 (`##`), subsections are H3+ — never jump levels
- [ ] Structural labels (Intro, Required Grants, FAQ, etc.) are exempt from imperative-mood rule

**Voice and tense**
- [ ] Present tense throughout ("This opens the panel" not "This will open the panel")
- [ ] Active voice
- [ ] "select" not "click" (except right-click, left-click, double-click)
- [ ] No exclamation points
- [ ] Numbers below 10 are spelled out

**Word choices to fix manually**
- [ ] `whitelist` → `allowlist`, `blacklist` → `blocklist`
- [ ] `utilize` → `use`
- [ ] `once` as a causal connector → `after`
- [ ] No Latin abbreviations (`i.e.`, `e.g.`, `etc.`) — use plain English equivalents
- [ ] No "verbiage" — use "words"
- [ ] No "Dojo" — use "Community Forums"
- [ ] No "KPI card" — use "Visualization Card"
- [ ] No "image card" — use "Doc Card"
- [ ] No "Page" or "Page Filters" — use "Dashboard" / "Dashboard Filters"
- [ ] No "Domo story/stories" — use "Dashboard/Dashboards"
- [ ] No "Drilldown" — use "Drill Path" or "drill into"
- [ ] No "Slicers" — use "Quick Filters"

**Beta markers**

Convert any legacy beta indicators to the current convention from `Domo-KB-Style-Guide.mdx` › **Beta Features**:

- [ ] `(Beta)` or `(BETA)` in a `title:` frontmatter value → remove and add `tag: "Beta"` to frontmatter
- [ ] `(Beta)` or `(BETA)` appended to a heading → remove and append `<Badge className="text-primary bg-primary/10 font-bold">Beta</Badge>`
- [ ] Ad-hoc beta notes → replace with the standard `<BetaNote />` component
- [ ] References to `betafeedback@domo.com` or `betadmin@domo.com` → replace with `beta.admin@domo.com`

**Punctuation and formatting**
- [ ] Em-dashes in body text: no spaces — `tools—such as these—work`
- [ ] Em-dashes in description lists: spaces inside the bold — `**Term —** description`
- [ ] Oxford comma in lists of three or more
- [ ] Bold static UI elements (`**Save**`, `**Admin** > **Security**`) — do not bold the `>`
- [ ] Table columns padded with spaces so pipes align vertically

**Domo terminology (capitalize exactly)**

| Correct | Never use |
|---------|-----------|
| DataSet | Dataset, dataset |
| DataFlow | Dataflow |
| DataFusion | Data Fusion |
| Magic ETL | magic ETL |
| Beast Mode | Beastmode, beast mode |
| AppDB | App DB, appDB |
| Dashboard | Page, Domo story |
| Dashboard Filters | Page Filters |
| Data Center | data center |
| Alerts Center | alerts center |
| Drill Path / drill into | Drilldown |
| Visualization Card | KPI card |
| Doc Card | image card |
| Community Forums | Dojo |
| Quick Filters | Slicers |
| Scheduled Reports | scheduled reports |
| Pro-code Editor | Procode Editor |

---

## Review Procedure

When invoked, do the following:

1. **Read the file** at the path provided in `$ARGUMENTS`. If a title is given instead, find the file with `grep -r "title:.*<title>" s/article/`.
2. **Scan for human-review items** from Section B above.
3. **Check image placeholders** — search for `<!-- TODO: embed image` comments and flag them.
4. **Check image-path case collisions** — for any image the article references under `images/kb/`, run `git ls-files images/kb/ | grep -i "<basename>"` to confirm no case-sibling exists.
5. **Check for leftover Salesforce artifacts** — any remaining `— | —`, bare Salesforce URLs, or raw HTML tags that markdownify didn't convert.
6. **Apply fixes** for items from Section B that are clearly wrong (e.g., incorrect Domo terminology, `whitelist` → `allowlist`).
7. **List items requiring editorial judgment** (e.g., gerund headings, passive-voice sentences) with line numbers so the user can decide.
8. **Write the corrected file** and report what was changed vs. what still needs the user's review.
