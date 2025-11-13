# IMAPFilter Helper

A helper CLI that orchestrates cache building, rule evaluation, and action execution for [IMAPFilter](https://github.com/lefcha/imapfilter).

## Layout

```
.
├── core/              # Core modules and the CLI entrypoints
├── data/              # Default location for generated artefacts (cache DB, logs, secrets)
├── rules/             # JSON rule files consumed by the helper
├── imapfilter_helper.py
└── tests/
```

The CLI parser and command implementations live in `core/cli.py`. The top-level `imapfilter_helper.py` is now a thin wrapper that simply calls `core.cli.main()`.

## Usage

Invoke the CLI via the module or script:

```bash
python -m core.cli <command> [flags]
# or
./imapfilter_helper.py <command> [flags]
```

### Commands

| Command | Purpose | Key flags |
| --- | --- | --- |
| `build-cache` | Fetches mail from the IMAP server and stores a local cache in `data/cache.db`. | `--all-folders` – scan every folder instead of just `INBOX`.<br>`--folder NAME` – cache only the specified folder (repeatable). |
| `evaluate` | Loads rules from `rules/` and evaluates them against the cached messages, enqueueing matching actions. | `--dry-run` – report matches without mutating the database.<br>`--all-folders` – consider every cached folder.<br>`--folder NAME` – evaluate only the selected folder(s). |
| `execute` | Executes any queued actions against the IMAP server. | `--dry-run` – preview without performing IMAP writes.<br>`--strict` – abort if required IMAP operations are missing or fail.<br>`--verbose` – log IMAP server replies and per-message progress for troubleshooting.<br>`--limit` – process at most the specified number of pending actions.<br>`--all-folders` / `--folder` – limit execution to particular folders, mirroring `evaluate`. |
| `run-all` | Convenience command that runs `build-cache`, `evaluate`, and `execute` sequentially. | `--dry-run` – perform a full simulation without IMAP writes.<br>`--all-folders` – include every folder when building the cache.<br>`--folder NAME` – restrict all three phases to the specified folder(s).<br>`--strict` – stop on missing/failed IMAP operations during execute.<br>`--verbose` – surface detailed progress and IMAP replies during the evaluate/execute phases.<br>`--limit` – cap how many pending actions are executed during the final phase. |
| `clear-pending` | Removes all pending actions from the queue without contacting the IMAP server. | *(no flags)* |
| `clear-cache` | Deletes cached message headers, folder metadata, and any queued actions. | *(no flags)* |

When no folder flags are supplied the `evaluate` and `execute` commands operate on every cached message by default. This makes it easy to build the cache for several folders (either in one run with repeated `--folder` flags or by invoking `build-cache` multiple times) and then apply the rules to the aggregated cache in a later step.

Use `clear-cache` whenever you need to discard the existing cache database and start again.

### Rule management console

A standalone, text-based console is available for creating and editing rule files without touching JSON by hand:

```bash
python rule_manager.py
```

The console automatically discovers files under `rules/` and offers keyboard shortcuts for each option:

* **Create / edit rules** – interactively manage the rule name, priority, conditions, actions, and any extra JSON fields.
* **Arrow-key navigation everywhere** – every menu and editor supports arrow keys, page navigation, and hotkeys for quick input.
* **Scrollable rule browser** – navigate long rule lists with arrow keys, page-up/down, or quick keyboard shortcuts.
* **Condition editor** – build nested `ALL`/`ANY` groups, add new header matchers, or tweak existing ones.
* **Action editor** – adjust the primary action settings (type/target) and supply additional fields when needed.
* **Priority management** – reorder rules with move-up/move-down shortcuts or jump straight to editing a priority value.
* **Dry-run testing** – run a single rule against the cached headers to preview how many messages it would match.

Backups are written automatically when deleting a rule, making it easy to undo an accidental removal.

### Configuration and data locations

The helper stores its cache database, log file, and secrets JSON under `data/` by default. Rules continue to be loaded from the `rules/` directory. These locations can be customised by constructing an `AppConfig` via `core.config.build_default_config()` with a different base directory.

To get started quickly, copy `data/secrets.example.json` to `data/secrets.json` and replace the placeholder IMAP credentials with your own.

## Development

Install the dependencies and run the tests:

```bash
pip install -r requirements.txt  # if available
pytest
```

### Diagnostic helpers

Two standalone scripts live under `core/tools/` to assist with troubleshooting:

* `python -m core.tools.sample_messages` – interactively copies random messages from `INBOX` into the `Test` folder for manual inspection.
* `python -m core.tools.move_diagnostics [--destination MAILBOX] [--ensure-destination]` – appends fresh test messages to `INBOX` and exercises the `UID MOVE` and fallback copy/delete flows, logging the IMAP server responses and verifying that each message arrives in the destination folder.

Both tools reuse the credentials and logging configuration from `data/secrets.json` and `data/log.json` respectively.
