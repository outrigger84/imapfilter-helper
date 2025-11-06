# IMAPFilter Helper

A helper utility that wraps common IMAPFilter workflows (building a local
cache, evaluating rules, and executing actions) with additional safeguards and
progress reporting.

## Project layout

```
.
├── imapfilter/
│   ├── __init__.py            # Package entry point exporting ``main``
│   ├── core/
│   │   ├── __init__.py        # Re-exports the command runner
│   │   └── app.py             # Full implementation of the helper
│   └── data/
│       ├── .gitignore         # Keeps runtime files out of version control
│       └── rules/             # JSON rule definitions bundled with the project
├── imapfilter_helper.py       # Thin CLI wrapper around ``imapfilter.main``
└── README.md
```

## Data directory

All runtime artefacts are stored beneath `imapfilter/data/`:

- `rules/` – bundled JSON rule files shipped with the repository.
- `cache.db` – created on demand the first time the cache is built.
- `secrets.json` – expected to be provided locally with IMAP credentials.
- `imapfilter-helper.log` – JSON log output written during execution.

The database, secrets file, and log file are intentionally ignored by Git. They
will be generated automatically or supplied locally when you run the helper.

## Usage

Run the command-line interface via the thin wrapper script:

```bash
./imapfilter_helper.py run-all --dry-run
```

The script dispatches into the `imapfilter` package, so you may also import and
call `imapfilter.main()` from Python code if you prefer to integrate it into a
larger system.
