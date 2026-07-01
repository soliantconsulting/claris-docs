# claris-docs

Community-maintained Claude Code plugin for Claris product documentation. **Not affiliated with Claris International Inc.**

These skills make Claude an authoritative source for Claris/FileMaker questions by fetching official documentation on demand from help.claris.com rather than relying on training data.

## Installation

Add the GitHub repo as a marketplace source, then install:

```
/plugin marketplace add soliantconsulting/soliant-public-claude-plugins
/plugin install claris-docs@soliant-public-claude-plugins
```

Once installed, skills are available as `claris-docs:data-api`, `claris-docs:filemaker-pro`, etc. Skills also trigger automatically when Claude detects a relevant Claris/FileMaker question.

### Uninstalling

```
/plugin uninstall claris-docs@soliant-public-claude-plugins
```

### Updating

Auto-update is off by default for third-party plugins. To enable it, run `/plugin`, go to the **Marketplaces** tab, select `soliant-public-claude-plugins`, and toggle **Enable auto-update**. When enabled, Claude Code updates the plugin at startup and prompts you to run `/reload-plugins`.

To update manually:

```
/plugin marketplace update soliant-public-claude-plugins
```

### Local development

To load the plugin from a local directory without installing it (for development and testing):

```bash
claude --plugin-dir /path/to/claris-docs
```

This loads the plugin for that session only and does not affect your installed version.

### Available skills

| Skill | Topics                                                                                                 |
|-------|--------------------------------------------------------------------------------------------------------|
| `claris-docs:filemaker-pro` | Scripting, functions, layouts, relationships, fields, security                                         |
| `claris-docs:filemaker-calc` | Debug/write/explain calculations - bundles an offline evaluator (`fmeval.py`) to run formulas for real |
| `claris-docs:filemaker-server` | Admin Console, hosting, backups, schedules, monitoring                                                 |
| `claris-docs:filemaker-cloud` | Cloud setup, administration, Claris ID                                                                 |
| `claris-docs:filemaker-go` | Mobile access, Go development, iOS App SDK                                                             |
| `claris-docs:filemaker-webdirect` | Browser-based access, deployment                                                                       |
| `claris-docs:claris-studio` | Views, fields, functions, teams                                                                        |
| `claris-docs:claris-connect` | Workflow automation, connectors                                                                        |
| `claris-docs:claris-customer-console` | Accounts, teams, subscriptions                                                                         |
| `claris-docs:claris-mcp` | AI assistant integration via MCP                                                                       |
| `claris-docs:data-api` | REST Data API, authentication, CRUD                                                                    |
| `claris-docs:admin-api` | Server administration REST API                                                                         |
| `claris-docs:odata` | OData protocol, BI tool integration                                                                    |
| `claris-docs:sql-reference` | ExecuteSQL, ODBC/JDBC syntax                                                                           |
| `claris-docs:security-guide` | Encryption, hardening, compliance                                                                      |
| `claris-docs:app-upgrade-tool` | FMUpgradeTool, patch files                                                                             |
| `claris-docs:data-migration-tool` | DMT, data migration                                                                                    |
| `claris-docs:developer-tool` | Developer Utilities, binding, runtime                                                                  |

## Project setup

For FileMaker projects, copy `CLAUDE.md.template` into your project root as `CLAUDE.md` (or append it to an existing one):

```sh
cp /path/to/claris-docs/CLAUDE.md.template CLAUDE.md
```

This gives Claude explicit routing rules so it always consults the right skill before answering from memory — more reliable than auto-trigger alone.

## How it works

Skills are navigators, not knowledge stores. They don't embed documentation content.

1. **SKILL.md** is loaded when a skill is invoked. It tells Claude how to use the reference files.
2. **`references/` files** contain page titles and URLs grouped by topic. Claude reads these to find the right page.
3. **Live fetch** — Claude fetches the actual documentation page from `help.claris.com/markdown/en/{guide}/{page}.md` at runtime.

This means answers always reflect the current published docs.

## Updating references

Reference files are an index of what's published at help.claris.com. When Claris adds, removes, or renames documentation pages, the references become stale — links may 404 or new content won't be discoverable. Run `update.py` to resync:

```sh
python update.py                          # fetch fresh llms-full.txt from Claris
```

Or

```sh
python update.py snapshots/llms-full.txt  # use a local snapshot
```

The script regenerates all `references/` files deterministically. Git diffs of these files show exactly what changed in Claris's docs.

## Design decisions

**Progressive disclosure** — skills don't dump 1000+ links into context. SKILL.md points to reference files; reference files point to live pages. Each level is loaded only when needed.

**Reference file sizing** — each file targets ≤80 links as a guideline – strongly encouraged, but not a hard limit. Under 80, a single file is fine (the AI can scan it cheaply). Over 80, split along natural topic boundaries where it makes sense. Over 150, always split. Rationale: each link is ~30-35 tokens; very large unsplit files waste context and risk the AI picking wrong pages. Exception: when no meaningful sub-split exists (e.g. `Get` functions, which are a single flat category with no sub-grouping), keeping them in one file is preferable to an artificial split. `pro-func-get.md` (140 links) is a documented exception.

**Link-list format** — reference files use the same `- [Title](URL)` format as the source `llms-full.txt`. Compact, clickable in editors, trivial to emit from update.py.

**Skills don't embed content** — Claris publishes LLM-friendly Markdown docs. Embedding them would create stale copies. Fetching live means one `update.py` run keeps the index current while content is always fresh.

**404 handling** — if a fetch fails, Claude tells the user the page may have moved and suggests running `update.py`. Skills never silently fail.
