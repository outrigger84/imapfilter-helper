# IMAPFilter Helper

A helper CLI that orchestrates cache building, rule evaluation, and action execution for [IMAPFilter](https://github.com/lefcha/imapfilter).

**Key Features:**
- Multi-cache support via `--cache-file` for testing and isolation
- Cache-assisted rule wizard with intelligent pattern suggestions
- Parallel cache building and execution (3-5x faster for multi-folder mailboxes)
- Stream-based processing for memory efficiency with large mailboxes
- Rule conflict detection and interactive resolution
- Interactive cache viewer and rule manager
- Message backup and recovery support
- Support for complex rule conditions with AND/OR/NOT logic

## Layout

```
.
├── core/                      # Core modules and the CLI entrypoints
│   ├── cli.py                 # Command parser and handlers
│   ├── cache_builder.py        # Header fetching and caching (supports parallel per-folder)
│   ├── rule_engine.py          # Rule loading and evaluation
│   ├── executor/               # Action execution against IMAP (serial + threaded parallel)
│   ├── stream_executor.py      # Stream-based execution for memory efficiency
│   ├── stream_processor.py     # Message streaming utilities
│   ├── rule_validator.py       # Rule JSON validation and schema checking
│   ├── imap_client.py          # IMAP protocol wrapper
│   ├── database.py             # SQLite schema and migrations
│   ├── backup.py               # Message backup utilities (mbox format)
│   ├── keywords.py             # IMAP keywords and flag management (with batching)
│   ├── connection_pool.py      # Connection pooling for performance
│   ├── config.py               # Configuration management
│   ├── logging_utils.py        # JSON logging and performance tracking
│   ├── ui_components.py        # TUI helper components
│   ├── wizard_cache.py         # 6-hour persistent cache for wizard operations
│   └── tools/                  # Utilities and helpers
│       ├── rule_wizard_core/   # Rule wizard core logic (selector, builder, wizard, …)
│       ├── cache_viewer.py     # Interactive cache browser with sorting/filtering
│       ├── coverage_analyzer.py# Pattern coverage analysis for rules
│       ├── sample_messages.py  # Copy test messages for inspection
│       └── move_diagnostics.py # Test IMAP move operations and verify success
├── data/                       # Generated artifacts (auto-created)
│   ├── secrets.json            # IMAP credentials (user-created from .example)
│   ├── cache.db                # SQLite cache database
│   ├── imapfilter-helper.log   # JSON-formatted logs (JSON-structured)
│   ├── wizard_cache.json       # Persistent cache for rule wizard suggestions
│   ├── keywords.json           # Predefined keywords list
│   └── backups/                # Message backup mbox files
├── rules/                      # JSON rule files (user-managed)
├── scripts/                    # Standalone utilities and diagnostics
│   ├── merge_worker_dbs.py     # Recovery fallback for failed parallel cache merges
│   └── diagnostics/            # Live-server connectivity checks
├── imapfilter_helper.py        # Main entry point script
├── rule_manager.py             # Interactive rule editor console (TUI)
├── rule_wizard.py              # Cache-assisted guided rule creator
├── server_probe.py             # IMAP server capability/performance probe (wraps core/server_probe.py)
├── wizard.py                   # Rule wizard entry point
└── tests/                      # Test suite (25 test files, 360+ test functions)
```

The CLI parser and command implementations live in `core/cli.py`. The top-level `imapfilter_helper.py` is now a thin wrapper that simply calls `core.cli.main()`.

## Setup

### Prerequisites

- Python 3.7+
- IMAP-enabled email account
- IMAP credentials (username/password)

### Installation

1. Clone the repository and navigate to the directory:
   ```bash
   git clone <repo-url>
   cd imapfilter
   ```

2. Install dependencies (if available):
   ```bash
   pip install -r requirements.txt
   ```

3. Set up IMAP credentials:
   ```bash
   cp data/secrets.example.json data/secrets.json
   # Edit data/secrets.json and replace with your IMAP server details
   ```

4. Verify connectivity (optional):
   ```bash
   ./imapfilter_helper.py build-cache --limit 1
   ```

The `data/` directory is created automatically if it doesn't exist. Rules can be managed via the interactive console or by creating JSON files directly in the `rules/` directory.

## Usage

Invoke the CLI via the module or script:

```bash
python -m core.cli [global-options] <command> [flags]
# or
./imapfilter_helper.py [global-options] <command> [flags]
```

### Global Options

All commands support these global options:

| Option | Purpose |
| --- | --- |
| `--cache-file PATH` | Use a custom cache database instead of the default `data/cache.db`. Allows maintaining multiple independent cache instances for testing, isolation, or parallel processing. Path is resolved relative to the current directory. Works with all commands. |

**Examples:**
```bash
# Use test cache for evaluation
python -m core.cli --cache-file data/cache-test.db evaluate --verbose

# Build separate cache for specific folder
python -m core.cli --cache-file data/cache-inbox.db build-cache --folder INBOX

# Run wizard with custom cache for pattern discovery
python rule_wizard.py --cache-file data/cache-discovery.db

# Process different folders with isolated caches
python -m core.cli --cache-file data/cache-work.db run-all --folder Work
python -m core.cli --cache-file data/cache-personal.db run-all --folder Personal
```

### Commands

| Command | Purpose | Key flags |
| --- | --- | --- |
| `build-cache` | Fetches mail headers from the IMAP server and stores a local cache. **Fast** – headers only, no message bodies. Supports parallel processing for multiple folders. | `--all-folders` – scan every folder instead of just `INBOX`.<br>`--folder NAME` – cache only the specified folder **(can be repeated for multiple folders)**.<br>`--folder-recursive NAME` – cache folder and all subfolders recursively **(can be repeated)**.<br>`--limit N` – cache at most N messages per folder.<br>`--order newest\|oldest\|random` – which messages to cache when limiting.<br>`--parallel-workers N` – number of parallel IMAP connections (default: auto-detect, use 1 to force sequential).<br>`--per-folder` – cache folders one at a time, continuing past failed folders (exit code 1 if any fail). |
| `evaluate` | Loads rules from `rules/` and evaluates them against the cached messages, enqueueing matching actions. | `--dry-run` – report matches without mutating the database.<br>`--all-folders` – consider every cached folder.<br>`--folder NAME` – evaluate only the selected folder(s).<br>`--folder-recursive NAME` – evaluate folder and all subfolders recursively.<br>`--verbose` – show detailed match information.<br>`--debug-headers` – log message headers for troubleshooting.<br>`--per-folder` – evaluate folders one at a time, continuing past failed folders (exit code 1 if any fail). |
| `execute` | Executes any queued actions against the IMAP server. **Can backup messages before moving them.** | `--dry-run` – preview without performing IMAP writes.<br>`--backup-moved` – backup messages before moving (recommended).<br>`--backup-all` – backup all cached messages after moves complete.<br>`--strict` – abort if required IMAP operations are missing or fail.<br>`--verify-moves` – confirm moves by searching for Message-ID.<br>`--verbose` – log IMAP server replies and per-message progress.<br>`--limit` – process at most this many pending actions.<br>`--all-folders` / `--folder` – limit execution to particular folders.<br>`--parallel-workers N` – number of parallel workers for execution.<br>`--per-folder` – execute folders one at a time, continuing past failed folders (exit code 1 if any fail). |
| `run-all` | Convenience command that runs `build-cache`, `evaluate`, and `execute` sequentially. **Optimized** – much faster than old --backup approach. | `--dry-run` – perform a full simulation without IMAP writes.<br>`--all-folders` – include every folder.<br>`--folder NAME` – restrict all three phases to the specified folder(s).<br>`--cache-limit` / `--cache-order` – cache-limiting flags.<br>`--backup-moved` – backup messages before moving them (recommended).<br>`--backup-all` – create full mbox archive after all moves complete.<br>`--debug-headers` – log message headers while evaluating rules.<br>`--strict` / `--verify-moves` – execute integrity checks.<br>`--verbose` – detailed progress and IMAP replies.<br>`--action-limit` – cap pending actions executed.<br>`--parallel-workers N` – control parallelization during execute phase.<br>`--per-folder` – complete all three phases on one folder before moving to the next (folder-major instead of phase-major; alphabetical order, continues past failed folders with exit code 1). |
| `eval-execute` | Evaluate rules and execute actions in sequence **without rebuilding the cache** (requires existing cache). Faster for re-evaluating with new rules. | `--dry-run` – simulate without IMAP writes.<br>`--all-folders` – process all cached folders.<br>`--folder NAME` – process only selected folder(s).<br>`--folder-recursive NAME` – process folder and subfolders recursively.<br>`--verbose` – show detailed progress.<br>`--debug-headers` – log message headers during evaluation.<br>`--limit N` – process at most N pending actions.<br>`--backup-moved` / `--backup-all` – backup messages during execution.<br>`--strict` / `--verify-moves` – execute integrity checks.<br>`--parallel-workers N` – control parallelization.<br>`--per-folder` – complete evaluate and execute on one folder before moving to the next (alphabetical order, continues past failed folders with exit code 1). |
| `stream` | **Stream-process mode:** Read messages from IMAP → evaluate rules → execute actions in real-time, **without using a cache database**. Memory efficient for large mailboxes or one-time processing. | `--dry-run` – simulate without IMAP writes.<br>`--all-folders` – process every folder instead of just `INBOX`.<br>`--folder NAME` – process only specified folder(s).<br>`--folder-recursive NAME` – process folder and all subfolders recursively.<br>`--verbose` – show detailed progress and matches.<br>`--limit N` – process at most N messages per folder.<br>`--backup-moved` – backup messages before moving them. |
| `check-conflicts` | Detect and resolve rule conflicts (priority ordering, unreachable rules, redundant patterns). Supports cache-based validation or static analysis. | `--validation-mode cache\|static\|prompt` – how to validate rules (default: prompt).<br>`--output detailed\|summary\|json` – output format (default: detailed).<br>`--conflict-types all\|priority\|unreachable\|redundant` – which conflicts to report (default: all).<br>`--severity all\|high\|medium\|low` – minimum severity level to report (default: all).<br>`--auto-fix` – enable interactive resolution workflow.<br>`--export PATH` – export conflict report to JSON file. |
| `clear-pending` | Removes all pending actions from the queue without contacting the IMAP server. | *(no flags)* |
| `clear-cache` | Deletes cached message headers, folder metadata, and any queued actions. | *(no flags)* |
| `compact-cache` | Removes cached headers for messages whose actions have already been executed so future evaluations skip them. | *(no flags)* |
| `keywords` | Manage predefined IMAP keywords and custom flags. Subcommands: `list` (show all), `add KEYWORD`, `remove KEYWORD`, `edit` (in default editor). | *(varies by subcommand)* |
| `view-cache` | Interactive browser for exploring and analyzing cached emails. Sort by sender, subject, date, or folder. Search and filter messages in real-time. | `--limit N` – maximum emails to load (default: 1000).<br>`--folder NAME` – filter by folder name. |

**Folder selection note:** When using `build-cache`, choose one of these mutually exclusive options:
- **`--all-folders`** – Cache every folder on the server (fastest for small mailboxes, slower for large ones)
- **`--folder NAME`** – Cache only specific folders, repeatable for multiple: `--folder INBOX --folder Sent`
- **`--folder-recursive NAME`** – Cache a folder and all subfolders, repeatable: `--folder-recursive Work`
- ***(default)*** – If no folder flags supplied, caches only `INBOX`

When no folder flags are supplied to the `evaluate` and `execute` commands, they operate on every cached message by default. This makes it easy to build the cache for several folders (either in one run with repeated `--folder` flags or by invoking `build-cache` multiple times) and then apply the rules to the aggregated cache in a later step.

**Per-folder mode:** By default the pipeline is phase-major – each phase (cache, evaluate, execute) runs across all selected folders before the next phase starts. Pass `--per-folder` to make it folder-major: all phases complete on one folder before the next folder starts, so results land incrementally. Folders are processed alphabetically and sequentially (an explicit `--parallel-workers N` still allows splitting a single large folder during cache builds). A folder that fails is logged and skipped, the run continues with the remaining folders, and the command exits with code 1 with the failed folders listed in the summary.

Use `clear-cache` whenever you need to discard the existing cache database and start again.

The `compact-cache` helper is a lighter-weight alternative for day-to-day use: run it after finishing an `execute` pass (or as a follow-up when re-running `evaluate` with new rules) to prune header rows tied to completed or skipped actions. This keeps the cache aligned with the mailbox contents and prevents stale matches from reappearing without forcing a full rebuild.

### Backup strategies

The helper offers two backup approaches during the `execute` phase:

- **`--backup-moved`** (recommended): Backs up only the messages that will be moved, immediately before moving them. This ensures you have a safety net without the performance cost of backing up your entire mailbox. **Much faster** than full backup for typical workflows.

- **`--backup-all`**: Backs up all cached messages after moves complete. Use this for periodic full archives. Can be combined with `--backup-moved` for maximum safety.

**Performance comparison** (50K message mailbox, 10% matched by rules):

```
# OLD approach (backup during cache build): ~52 minutes
./imapfilter_helper.py run-all --backup

# NEW approach (selective backup): ~14 minutes (73% faster!)
./imapfilter_helper.py run-all --backup-moved

# NEW approach (full archive): ~14 minutes active + background
./imapfilter_helper.py run-all --backup-all
```

**Why is this faster?**
1. Caching headers only (no message bodies) is 8-10x faster
2. Evaluation and rule matching can start immediately
3. Backup happens in parallel with moves, or only for affected messages

**Safety**: When `--backup-moved` is used, the expunge operation only occurs after messages are successfully backed up. If backup fails, moves are aborted to prevent data loss.

### Rule format and structure

Rules are JSON files stored in the `rules/` directory. Each rule defines conditions to match email headers and actions to take on matched messages.

**Basic rule structure:**

```json
{
  "name": "Rule display name",
  "priority": 100,
  "conditions": {
    "all": [
      {
        "header": "from",
        "contains": "example@domain.com"
      }
    ]
  },
  "action": {
    "type": "move",
    "target": "TargetFolder"
  }
}
```

**Condition matching:**

- **`contains`** – substring match (case-insensitive)
- **`regex`** – regular expression match against header value
- **`all`** – all nested conditions must match (AND logic)
- **`any`** – at least one nested condition must match (OR logic)

Common headers: `from`, `to`, `cc`, `subject`, `date`, `message-id`

**Negation operators:**

- **`not_contains`** – negated substring match (excludes if present)
- **`not_equals`** – negated exact match (case-insensitive)
- **`not_regex`** – negated regular expression match
- **`equals`** – exact match (case-insensitive)

**Example with negation - Exclude specific sender:**

```json
{
  "name": "Amazon except receipts",
  "conditions": {
    "all": [
      {"header": "from", "contains": "@amazon.com"},
      {"header": "from", "not_contains": "receipts@"}
    ]
  },
  "action": {
    "type": "move",
    "target": "Shopping"
  }
}
```

**NOT wrapper - Negate complex conditions:**

- **`not`** – wrapper that inverts any condition or boolean group

```json
{
  "name": "Not from spam domains",
  "conditions": {
    "not": {
      "any": [
        {"header": "from", "regex": "@spam\\.com$"},
        {"header": "from", "regex": "@junk\\.com$"}
      ]
    }
  },
  "action": {
    "type": "move",
    "target": "Inbox"
  }
}
```

**Example with nested conditions:**

```json
{
  "name": "Newsletters and promotions",
  "priority": 50,
  "conditions": {
    "any": [
      {
        "all": [
          {"header": "from", "regex": "noreply@.*\\.com"},
          {"header": "subject", "contains": "newsletter"}
        ]
      },
      {"header": "from", "contains": "promo@"}
    ]
  },
  "action": {
    "type": "move",
    "target": "Newsletters"
  }
}
```

### Common filtering patterns with negation

**Exclude specific senders from a domain:**

```json
{
  "name": "Company emails except no-reply",
  "conditions": {
    "all": [
      {"header": "from", "contains": "@company.com"},
      {"header": "from", "not_equals": "noreply@company.com"}
    ]
  },
  "action": {"type": "move", "target": "Company"}
}
```

**Include everything except spam indicators:**

```json
{
  "name": "Clean inbox (no spam)",
  "conditions": {
    "not": {
      "any": [
        {"header": "subject", "regex": "\\[SPAM\\]"},
        {"header": "from", "contains": "no-reply"},
        {"has_keyword": "Junk"}
      ]
    }
  },
  "action": {"type": "move", "target": "Inbox"}
}
```

**Newsletter filtering with multiple exclusions:**

```json
{
  "name": "Newsletters (excluding promotions)",
  "conditions": {
    "all": [
      {"header": "from", "regex": "newsletter@.*\\.com"},
      {"header": "subject", "not_contains": "unsubscribe"},
      {"header": "subject", "not_contains": "promo"},
      {"header": "subject", "not_contains": "sale"}
    ]
  },
  "action": {"type": "move", "target": "Newsletters"}
}
```

**Combine positive and negative flags:**

```json
{
  "name": "Unread important messages",
  "conditions": {
    "all": [
      {"has_keyword": "important"},
      {"not": {"has_keyword": "\\Seen"}}
    ]
  },
  "action": {"type": "set_keywords", "keywords": ["priority"]}
}
```

### When to use which operator

| Scenario | Use This | Example |
|----------|----------|---------|
| Exclude one substring | `not_contains` | `{"header": "from", "not_contains": "spam"}` |
| Exclude exact value | `not_equals` | `{"header": "from", "not_equals": "noreply@"}` |
| Exclude pattern | `not_regex` | `{"header": "from", "not_regex": "^spam@"}` |
| Match exact value | `equals` | `{"header": "from", "equals": "user@example.com"}` |
| Negate complex OR group | `not` wrapper | `{"not": {"any": [...]}}` |
| Negate complex AND group | `not` wrapper | `{"not": {"all": [...]}}` |
| Negate flag condition | `not` wrapper | `{"not": {"has_keyword": "\\Seen"}}` |

**Priority:** Lower numbers execute first. Use priorities 1-1000 for flexibility.

**Action types:**

- **`"move"`** – Move the message to a target folder. Specifies `"target"` field with folder name.
- **`"set_keywords"`** – Set IMAP keywords/flags on the message. Specifies `"keywords"` field with array of keyword names.
  - Common keywords: `"\\Seen"`, `"\\Answered"`, `"\\Flagged"`, `"\\Draft"`, `"\\Deleted"`, or custom keywords like `"receipts"`, `"important"`, etc.
  - Keywords are processed efficiently in batch operations to minimize IMAP server round trips.

**Example with keyword action:**

```json
{
  "name": "Mark important emails",
  "conditions": {
    "all": [
      {"header": "from", "regex": "@company\\.com"},
      {"header": "subject", "contains": "urgent"}
    ]
  },
  "action": {
    "type": "set_keywords",
    "keywords": ["\\Flagged", "important"]
  }
}
```

**Example with multiple keywords in condition:**

```json
{
  "name": "Process unread messages",
  "conditions": {
    "all": [
      {"has_keyword": "\\Seen", "not": true},
      {"has_keyword": "important", "equals": true}
    ]
  },
  "action": {
    "type": "set_keywords",
    "keywords": ["\\Flagged"]
  }
}
```

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

### Cache-Assisted Rule Wizard

A new interactive guided tool for creating rules using intelligent suggestions from your cached emails:

```bash
python rule_wizard.py
```

**Key features:**

* **Cache-powered suggestions** – Shows actual senders, recipients, and subjects from your mailbox with message counts
* **Real-time search/filter** – Type to search through long lists of senders or subjects
* **Smart pattern extraction** – Automatically suggests patterns:
  - Email patterns: exact match, wildcard TLD (`user@amazon.*`), domain only (`@amazon.com`), or domain base (`amazon`)
  - Subject patterns: exact match, without numbers (removes order IDs), first N words, or keywords
* **Message count preview** – Each pattern shows how many messages it would match
* **Multiple conditions** – Add multiple conditions with AND (all) or OR (any) logic
* **Dry-run preview** – See how many cached messages would match before saving
* **Auto-generated filenames** – Rules are saved with sequential IDs and descriptive names

**Workflow:**

1. Cache validation – Confirms `data/cache.db` exists and has messages
2. Add conditions – Interactively build conditions using cache-assisted selection
3. Configure action – Set target folder for matched messages
4. Set metadata – Name and priority for the rule
5. Preview & save – Review generated JSON and dry-run match count, then save

**Example session:**

```
Rule name: Newsletters - Reddit
[Filterable list of top senders appears]
Filter: redd
    1. Reddit <noreply@redditmail.com> (610 messages)
    2. Reddit <community@reddit.com> (124 messages)

Selected: noreply@redditmail.com

Suggested patterns:
    1. noreply@redditmail.com (exact - 610 messages)
    2. noreply@reddit.* (all TLDs - 610 messages) [RECOMMENDED]
    3. @reddit.com (all from domain - 1,203 messages)
    4. reddit (all reddit domains - 1,203 messages)

Select pattern or Enter to use exact: 2
Match type: Contains (1) or Regex (2)? 1

Add another condition? (yes/no): no

Target folder: Newsletters/Reddit
Priority [100]:
Rule name (auto-suggested): Newsletters - Reddit

Preview: Would match 610 of 12,186 cached messages
Save? (yes/no/edit): yes
✓ Saved to rules/99013_newsletters_reddit.json
```

**When to use:**

- **New to rule creation** – Guided experience with intelligent suggestions
- **Want to avoid JSON editing** – Complete interactive interface
- **Need pattern help** – See actual email addresses/subjects to create better rules
- **Want confidence** – Preview match counts before committing

### Interactive Cache Viewer

A tool for visually exploring and analyzing your cached email messages:

```bash
python -c "from core.tools.cache_viewer import main; main()"
```

**Key features:**

* **Interactive browser** – Navigate cached messages with arrow keys and keyboard shortcuts
* **Sorting options** – Sort by sender, subject, date, or message count
* **Real-time filtering** – Instantly filter emails by sender, subject, or any text
* **Message counts** – See how many emails match each pattern (useful for testing rule patterns)
* **Scrolling** – Smooth navigation through large lists with page-up/page-down support
* **Pattern matching** – Identify patterns in your emails to craft more effective rules

**Use cases:**

- Explore what emails are available in your cache
- Test pattern matching for new rules before creating them
- Identify top senders and subjects for rule creation
- Verify that your cache building worked correctly
- Debug why a rule might or might not be matching

### Configuration and data locations

The helper stores its cache database, log file, and secrets JSON under `data/` by default. Rules continue to be loaded from the `rules/` directory.

**Default locations:**
- Cache: `data/cache.db`
- Logs: `data/imapfilter-helper.log`
- Secrets: `data/secrets.json`
- Rules: `rules/`

**Custom cache locations:**

Use the `--cache-file` argument to specify a custom cache path:

```bash
# Alternate cache location in same directory
python -m core.cli --cache-file data/cache-backup.db build-cache

# Absolute path
python -m core.cli --cache-file /tmp/cache.db build-cache

# Relative path (relative to current directory)
python -m core.cli --cache-file ../other-project/cache.db build-cache

# Home directory
python -m core.cli --cache-file ~/mail-cache.db build-cache
```

To get started quickly, copy `data/secrets.example.json` to `data/secrets.json` and replace the placeholder IMAP credentials with your own.

The IMAP password can also be supplied via the `IMAPFILTER_PASSWORD` environment variable, which takes precedence over the secrets file — useful if you prefer not to keep the password on disk (the `password` key can then be omitted entirely).

**Secrets file structure (`data/secrets.json`):**

```json
{
  "imap": {
    "host": "imap.gmail.com",
    "port": 993,
    "username": "your.email@gmail.com",
    "password": "your-app-password"
  }
}
```

### Notifications

Push notifications are supported via **Gotify** and/or **Telegram**. Configure either or both under the `notifications` key in `data/secrets.json`:

```json
{
  "imap": { "..." : "..." },
  "notifications": {
    "gotify": {
      "enabled": true,
      "base_url": "http://gotify.example.com",
      "token": "your_application_token",
      "max_timeout_failures": 3
    },
    "telegram": {
      "enabled": true,
      "bot_token": "123456789:AABBCCDDEEFFaabbccddeeff-your-bot-token",
      "chat_id": "your_chat_id",
      "max_timeout_failures": 3,
      "min_priority": 2
    }
  }
}
```

**Telegram options:**

| Key | Default | Description |
| --- | --- | --- |
| `bot_token` | *(required)* | Telegram bot token from [@BotFather](https://t.me/BotFather). |
| `chat_id` | *(required)* | Chat or channel ID to send messages to. |
| `max_timeout_failures` | `3` | Number of consecutive HTTP timeouts before the notifier auto-disables for the rest of the run. |
| `min_priority` | `0` | Minimum event priority to forward to Telegram. Events below this threshold are silently skipped. **Recommended: `2`** for large jobs — suppresses per-action success noise (priority 1) while still delivering summaries (priority 2–3) and failures (priority 4–5). |

**Why `min_priority` matters on large jobs:** Telegram bots are rate-limited to ~30 messages per minute. Without filtering, a job that moves 500 emails generates 500 individual "Action Executed" notifications, which quickly exhausts the rate limit and floods the chat. Setting `min_priority: 2` reduces that to a single "Execute Complete" summary.

Notifications are dispatched asynchronously on a background thread so they never block or slow down the main execution pipeline. If Telegram returns a `429 Too Many Requests` response, the notifier automatically honours the `retry_after` cooldown window before attempting the next send.

To suppress all notifications for a single run, pass `--no-notifications`. To suppress only one notifier, use `--no-telegram` or `--no-gotify`.

Common IMAP servers:
- **Gmail**: `imap.gmail.com:993`
- **Outlook**: `imap-mail.outlook.com:993`
- **Apple**: `imap.mail.me.com:993`
- **Yahoo**: `imap.mail.yahoo.com:993`

### Typical workflows

**Single-run full automation:**

```bash
./imapfilter_helper.py run-all --backup-moved --verbose
```

This builds the cache for INBOX, evaluates all rules, and executes matched actions with backups.

**Multi-folder processing:**

The `--folder` flag can be repeated to cache multiple specific folders, making it easy to target exactly the folders you want:

```bash
# Cache multiple specific folders in one command
./imapfilter_helper.py build-cache --folder INBOX --folder Sent --folder Archive

# Then evaluate and execute for all cached folders
./imapfilter_helper.py evaluate
./imapfilter_helper.py execute --backup-moved

# Or combine everything in one step
./imapfilter_helper.py run-all --folder INBOX --folder Sent --folder Archive --backup-moved
```

**Combining folder selection with other options:**

```bash
# Cache multiple folders with a message limit
./imapfilter_helper.py build-cache --folder INBOX --folder Sent --limit 100

# Cache specific folders in newest-first order (useful for large mailboxes)
./imapfilter_helper.py build-cache --folder INBOX --folder Work --order newest

# Cache multiple folders with parallel processing for speed
./imapfilter_helper.py build-cache --folder INBOX --folder Sent --folder Archive --parallel-workers 3
```

**Combining regular and recursive folders:**

```bash
# Cache INBOX plus all subfolders under "Work"
./imapfilter_helper.py build-cache --folder INBOX --folder-recursive Work

# Cache INBOX, all of Work/, and all of Projects/
./imapfilter_helper.py build-cache --folder INBOX --folder-recursive Work --folder-recursive Projects
```

**Why use multiple `--folder` flags?**

- **Selective caching** – Only cache folders you need, reducing cache size and build time
- **Testing** – Cache a small set of folders to test rules before running on all folders
- **Staged processing** – Cache folders in one step, then evaluate and execute later
- **Large mailboxes** – Combine with `--limit` and `--order newest` to cache recent messages first

**Parallel cache building (speeding up large mailboxes):**

The `--parallel-workers` flag controls how many IMAP connections are used simultaneously to fetch messages. This dramatically speeds up cache building for mailboxes with many folders:

```bash
# Auto-detect optimal worker count (default behavior)
./imapfilter_helper.py build-cache --all-folders

# Force sequential processing (1 worker, slower but uses minimal bandwidth)
./imapfilter_helper.py build-cache --all-folders --parallel-workers 1

# Use 3 parallel workers (medium speed, moderate resource usage)
./imapfilter_helper.py build-cache --all-folders --parallel-workers 3

# Use 5 parallel workers (faster for large mailboxes with many folders)
./imapfilter_helper.py build-cache --all-folders --parallel-workers 5

# Combine with folder selection and limits
./imapfilter_helper.py build-cache --folder INBOX --folder-recursive Work --parallel-workers 4 --limit 1000
```

**Performance impact:**

- **Auto-detect (default)**: System automatically chooses based on folder count
  - 1-4 folders → sequential (1 worker)
  - 5+ folders → parallel (auto-detected worker count)
- **Sequential (--parallel-workers 1)**: ~1x speed, minimal CPU/bandwidth
- **Parallel (3-5 workers)**: **3-5x faster** for multi-folder mailboxes, balanced resource usage
- **High concurrency (6+ workers)**: Fastest but may hit IMAP server rate limits

**Choosing the right worker count:**

| Scenario | Recommendation | Reason |
|----------|---|---|
| Single folder (INBOX only) | 1 (default sequential) | No parallelism benefit |
| 2-5 folders | 2-3 workers | Good balance of speed vs. resource usage |
| 10+ folders, fast IMAP server | 4-5 workers | Maximizes parallelization |
| Large mailboxes (100K+ messages) | 3 workers + `--limit` | Reduce time per folder with limits |
| Network-limited (slow connection) | 1-2 workers | Reduce concurrent bandwidth |
| Rate-limited by IMAP server | 1 worker | Respect server throttling |

**Example: Fast caching workflow for large mailbox:**

```bash
# Cache all folders in parallel with a message limit
./imapfilter_helper.py build-cache --all-folders --limit 5000 --order newest --parallel-workers 5

# Then evaluate and execute
./imapfilter_helper.py evaluate --verbose
./imapfilter_helper.py execute --backup-moved
```

**Caching large mailboxes efficiently:**

```bash
# Cache only recent messages
./imapfilter_helper.py build-cache --all-folders --limit 5000 --order newest

# Then evaluate and execute
./imapfilter_helper.py evaluate
./imapfilter_helper.py execute --backup-moved
```

**Testing new rules safely:**

```bash
# Dry-run to see what would match
./imapfilter_helper.py evaluate --dry-run --verbose

# If looks good, preview execution
./imapfilter_helper.py execute --dry-run

# Then execute with backups
./imapfilter_helper.py execute --backup-moved
```

**Clean up stale cache:**

```bash
# After completing moves, remove completed action headers
./imapfilter_helper.py compact-cache

# Or start fresh if needed
./imapfilter_helper.py clear-cache
```

### Using Multiple Cache Instances

The `--cache-file` argument allows you to maintain separate, independent cache databases for different purposes. This is useful for testing, isolation, and parallel processing.

**Testing new rules safely with a separate cache:**

```bash
# Build a test cache with a sample of messages
./imapfilter_helper.py --cache-file data/cache-test.db build-cache --folder INBOX --limit 100

# Create and test new rules against the test cache
python rule_wizard.py --cache-file data/cache-test.db

# Evaluate new rules against test cache
./imapfilter_helper.py --cache-file data/cache-test.db evaluate --verbose

# Dry-run execution
./imapfilter_helper.py --cache-file data/cache-test.db execute --dry-run

# Once confident, run against production cache
./imapfilter_helper.py evaluate --verbose
./imapfilter_helper.py execute --backup-moved
```

**Isolating different folder hierarchies:**

```bash
# Separate cache for newsletters only
./imapfilter_helper.py --cache-file data/cache-newsletters.db build-cache --folder Newsletters --folder-recursive

# Separate cache for work emails
./imapfilter_helper.py --cache-file data/cache-work.db build-cache --folder-recursive "Work"

# Separate cache for personal emails
./imapfilter_helper.py --cache-file data/cache-personal.db build-cache --folder Personal

# Process each independently with their own rules
./imapfilter_helper.py --cache-file data/cache-newsletters.db run-all
./imapfilter_helper.py --cache-file data/cache-work.db run-all
./imapfilter_helper.py --cache-file data/cache-personal.db run-all
```

**Using the wizard to discover patterns:**

```bash
# Build a limited cache for pattern discovery
./imapfilter_helper.py --cache-file data/cache-discovery.db build-cache --limit 500

# Run the rule wizard with limited cache to explore patterns
python rule_wizard.py --cache-file data/cache-discovery.db

# Once rules are created and tested, use full production cache
./imapfilter_helper.py evaluate
```

**Parallel cache management:**

```bash
# Build multiple independent caches in sequence
./imapfilter_helper.py --cache-file data/cache-inbox.db build-cache --folder INBOX
./imapfilter_helper.py --cache-file data/cache-sent.db build-cache --folder Sent
./imapfilter_helper.py --cache-file data/cache-archive.db build-cache --folder Archive

# Or run evaluation on multiple caches with different rule sets
./imapfilter_helper.py --cache-file data/cache-inbox.db evaluate
./imapfilter_helper.py --cache-file data/cache-inbox.db execute --backup-moved
```

**When to use multiple caches:**

- **Testing rules** – Create test cache with sample messages before applying to production
- **Selective processing** – Maintain separate caches for folders that need different rule sets
- **Performance** – Build caches in parallel, then process them independently
- **Safety** – Experiment with new configurations without affecting production cache
- **Isolation** – Process different folder hierarchies completely independently
- **Cleanup** – Keep archive of old cache while building fresh one

### Troubleshooting

**Connection errors:**
- Verify IMAP credentials in `data/secrets.json`
- Check that your email provider has IMAP enabled
- For Gmail, use an [app password](https://support.google.com/accounts/answer/185833), not your account password
- Ensure your firewall allows outbound IMAP (port 993)

**No messages cached:**
- Run with `--verbose` flag to see IMAP folder listing: `./imapfilter_helper.py build-cache --verbose`
- Check that target folder exists on IMAP server
- Verify message limits aren't too restrictive: `--limit` parameter

**Rules not matching:**
- Use `--debug-headers` flag during evaluation to see cached headers
- Test rule with the interactive console: `python rule_manager.py` → select rule → "test this rule"
- Verify regex patterns with a regex tester tool
- Check header names against actual email (common headers: `from`, `subject`, `to`, `date`)

**Move operations failing:**
- Run with `--verify-moves` to confirm messages arrive in destination
- Ensure destination folder exists on IMAP server
- Use `--backup-moved` to ensure messages are backed up before deletion
- Check IMAP server logs for permission/quota issues
- Try `python -m core.tools.move_diagnostics` to test IMAP move operations

**Performance issues:**
- Use `--limit` to reduce messages processed initially
- Use `--order newest` to process recent messages first
- Use `--backup-moved` instead of `--backup-all` for faster execution
- Run `compact-cache` periodically to remove completed actions
- Consider filtering to specific folders with `--folder`

**Out of disk space:**
- Backups stored in `data/backups/` as mbox files
- Remove old backup files manually if space is needed
- Use `--backup-moved` (smaller) instead of `--backup-all` (larger)

**Custom cache file issues:**
- **"Cache not found" error** – Verify the path exists or use `build-cache` first to create it
- **Path not found** – Ensure parent directory exists: `mkdir -p data/subdirectory/`
- **Permission denied** – Check file permissions: `ls -l data/cache.db`
- **Tilde not expanding** – Use absolute paths instead: `~/cache.db` becomes `/home/user/cache.db`
- **Relative paths** – Are resolved relative to current directory, use absolute paths for clarity
- **Multiple cache instances** – Each cache is completely independent; changes to one don't affect others

## Development

### Setup and Testing

Install the dependencies and run the tests:

```bash
pip install -r requirements.txt
pytest   # collects tests/ only (see pyproject.toml)
```

### Key Development Modules

**Performance & Optimization:**
* `core/stream_executor.py` – Memory-efficient message processing for large mailboxes using streaming approach
* `core/stream_processor.py` – Message streaming utilities for batch processing
* `core/connection_pool.py` – IMAP connection pooling to reduce connection overhead during parallel operations
* `core/keywords.py` – Batched keyword/flag operations for efficient IMAP interactions

**Validation & Quality:**
* `core/rule_validator.py` – Validates rule JSON structure, schemas, and condition logic before execution
* `core/wizard_cache.py` – 6-hour persistent cache for wizard operations to avoid repeated IMAP queries

**User Experience:**
* `core/ui_components.py` – Reusable TUI components for interactive tools
* `core/tools/cache_viewer.py` – Interactive browser for cached emails (NEW)
* `core/tools/coverage_analyzer.py` – Pattern coverage analysis for rule optimization (NEW)

### Recent Optimizations

**Parallel Processing:**
- Cache building now processes multiple folders in parallel for 3-5x speedup
- Action execution uses threading with per-thread database isolation for safe concurrent operations

**Batch Operations:**
- Keyword operations batch process to reduce IMAP server round trips
- Connection pooling reuses IMAP connections across parallel tasks

**Smart Caching:**
- Wizard caches email metadata for 6 hours to avoid repeated IMAP queries
- Cache compaction removes completed actions to keep database lean
- SQLite index optimization for fast header lookups

### Diagnostic helpers

Several standalone scripts and test suites assist with troubleshooting and development:

**Diagnostic tools:**
* `python -m core.tools.sample_messages` – Interactively copies random messages from `INBOX` into the `Test` folder for manual inspection.
* `python -m core.tools.move_diagnostics [--destination MAILBOX] [--ensure-destination]` – Appends fresh test messages to `INBOX` and exercises the `UID MOVE` and fallback copy/delete flows, logging IMAP server responses and verifying message arrival.
* `python -c "from core.tools.cache_viewer import main; main()"` – Interactive cache browser for exploring cached emails with sorting, filtering, and message counts. Useful for understanding your mailbox structure and testing rule patterns.
* `python -m core.tools.coverage_analyzer` – Analyzes rule patterns and shows coverage statistics to identify gaps in your filtering rules.
* `python scripts/diagnostics/check_imap_connection.py` – Step-by-step IMAP connectivity check (socket, SSL, login). **Connects to the live server.**
* `python scripts/diagnostics/check_folder_status.py` – Runs STATUS against every folder to find one that hangs. **Connects to the live server.**
* `python scripts/merge_worker_dbs.py` – Recovery fallback: manually merge `thread_*.db` worker files if a parallel cache build fails to merge automatically.
* `python scripts/diagnostics/wizard_smoke.py` – Smoke-checks the rule wizard entry point against the real `data/` cache (imports, component init, cache validation).

All tools reuse the credentials and logging configuration from `data/secrets.json`.

**Testing infrastructure:**
* `pytest` – Run the test suite. Collection is scoped to `tests/` via `pyproject.toml`; scripts elsewhere named `test_*.py` are diagnostics, not tests.

One-off scripts from completed migrations and past debugging sessions live in `.archive/` for reference.
