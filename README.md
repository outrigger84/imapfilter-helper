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
| `build-cache` | Fetches mail from the IMAP server and stores a local cache in `data/cache.db`. | `--all-folders` – scan every folder instead of just `INBOX`. |
| `evaluate` | Loads rules from `rules/` and evaluates them against the cached messages, enqueueing matching actions. | `--dry-run` – report matches without mutating the database. |
| `execute` | Executes any queued actions against the IMAP server. | `--dry-run` – preview without performing IMAP writes.<br>`--strict` – abort if required IMAP operations are missing or fail. |
| `run-all` | Convenience command that runs `build-cache`, `evaluate`, and `execute` sequentially. | `--dry-run` – perform a full simulation without IMAP writes.<br>`--all-folders` – include every folder when building the cache.<br>`--strict` – stop on missing/failed IMAP operations during execute. |

### Configuration and data locations

The helper stores its cache database, log file, and secrets JSON under `data/` by default. Rules continue to be loaded from the `rules/` directory. These locations can be customised by constructing an `AppConfig` via `core.config.build_default_config()` with a different base directory.

## Development

Install the dependencies and run the tests:

```bash
pip install -r requirements.txt  # if available
pytest
```
