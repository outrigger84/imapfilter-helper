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
python -m core.cli run-all --dry-run
# or
./imapfilter_helper.py run-all --dry-run
```

The helper stores its cache database, log file, and secrets JSON under `data/` by default. Rules continue to be loaded from the `rules/` directory. These locations can be customised by constructing an `AppConfig` via `core.config.build_default_config()` with a different base directory.

## Development

Install the dependencies and run the tests:

```bash
pip install -r requirements.txt  # if available
pytest
```
