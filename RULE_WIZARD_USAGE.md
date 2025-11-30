# Rule Wizard Usage Guide

Complete guide to using the IMAPFilter cache-assisted rule creation wizard.

## Table of Contents

1. [Quick Start (5 minutes)](#quick-start-5-minutes)
2. [Installation & Setup](#installation--setup)
3. [Step-by-Step Walkthrough](#step-by-step-walkthrough)
4. [Real-World Examples](#real-world-examples)
5. [Pattern Suggestions Explained](#pattern-suggestions-explained)
6. [Features](#features)
7. [Keyboard Controls](#keyboard-controls)
8. [Common Tasks](#common-tasks)
9. [Troubleshooting](#troubleshooting)
10. [Advanced Usage](#advanced-usage)
11. [Tips & Tricks](#tips--tricks)

---

## Quick Start (5 minutes)

### What It Does

The Rule Wizard is an interactive terminal tool that guides you through creating email filter rules. It shows you real data from your mailbox (senders, subjects, etc.) and suggests patterns that match multiple similar messages.

**In plain English:** Instead of manually writing JSON rules, you pick from lists of your actual email senders and the wizard creates the rule file for you.

### Prerequisites

**You must build the cache first:**

```bash
cd /root/imapfilter
./imapfilter_helper.py build-cache
```

This fetches message headers from your IMAP mailbox and stores them locally (headers only, very fast).

### Run the Wizard

```bash
python rule_wizard.py
```

### Expected Output

```
================================================================
   IMAPFilter Rule Wizard
================================================================

--- Checking Cache --------------------------------------------

Found cache at: /root/imapfilter/data/cache.db
Found 12,345 cached messages

--- Step 1: Basic Information ---------------------------------

Rule name (e.g., 'Banking - NatWest'):
```

The wizard will guide you through:
1. Naming your rule
2. Adding conditions (who sent it, subject line, etc.)
3. Choosing a destination folder
4. Saving the rule

---

## Installation & Setup

### Location

The wizard is already installed at:
```
/root/imapfilter/rule_wizard.py
```

### Dependencies

All dependencies should already be installed. The wizard requires:
- Python 3.7+
- Standard library modules (curses, sqlite3, json)
- IMAPFilter core modules (included in this repo)

### Cache Requirement

**IMPORTANT:** The wizard requires a populated cache to work. Build it first:

```bash
# Build cache from INBOX only (faster)
./imapfilter_helper.py build-cache

# Or build from all folders (more data for pattern suggestions)
./imapfilter_helper.py build-cache --all-folders

# Or build from specific folders
./imapfilter_helper.py build-cache --folder INBOX --folder Sent
```

The cache contains message headers only (not full message bodies), so building is fast. For a typical inbox with 10,000 messages, expect cache building to take 1-3 minutes.

### Verify Setup

Check that the cache exists and has data:

```bash
ls -lh data/cache.db
# Should show a file with non-zero size
```

---

## Step-by-Step Walkthrough

### Starting the Wizard

```bash
cd /root/imapfilter
python rule_wizard.py
```

You'll see the welcome banner and cache validation:

```
================================================================
   IMAPFilter Rule Wizard
================================================================

--- Checking Cache --------------------------------------------

Found cache at: /root/imapfilter/data/cache.db
Found 12,345 cached messages
```

If the cache is missing or empty, you'll see an error with instructions to build it.

---

### Step 1: Basic Information

The wizard first asks for rule metadata:

```
--- Step 1: Basic Information ---------------------------------

Rule name (e.g., 'Banking - NatWest'): Banking » NatWest
Rule priority [100]: 150

  Name: Banking » NatWest
  Priority: 150
```

**Rule Name:** Use descriptive names with separators like `»` or `/`. Examples:
- `Banking » NatWest`
- `Newsletters » Reddit`
- `Shopping / Amazon`

**Priority:** Higher numbers run first. Default is 100. Use:
- 200+ for important filters
- 100 for most rules
- 50 or lower for catch-all rules

---

### Step 2: Adding Conditions

Next, you'll add one or more conditions that messages must match:

```
--- Step 2: Add Conditions ------------------------------------

Add conditions to match messages. You can add multiple conditions.

What type of condition would you like to add?
  1. From address (sender)
  2. To address (recipient)
  3. Subject line
  4. Other header field
  5. Done adding conditions

Select option [1-5]: 1
```

#### Option 1: From Address (Sender)

After selecting "From address", the wizard loads all unique senders from your cache:

```
--- From Address Condition ------------------------------------

Loading top senders from cache...
Found 1,234 unique senders.
```

**Filterable List Selector** opens (see screenshot below):

```
Select Sender (1,234 items)
Filter:

[all 1,234 items]

>     1. noreply@amazon.com (245)
      2. notifications@github.com (189)
      3. info@natwest.com (87)
      4. noreply@reddit.com (76)
      ...

↑/↓ navigate  Enter select  Type to filter  Backspace delete  ESC cancel
```

**Type to filter in real-time:**

```
Select Sender (1,234 items)
Filter: nat

[showing 3 of 1,234]

>     1. info@notifications.natwest.com (64)
      2. noreply@natwest.com (23)
      3. natwestinternational@natwest.com (1)

↑/↓ navigate  Enter select  Type to filter  Backspace delete  ESC cancel
```

Press **Enter** to select the highlighted item.

#### Pattern Suggestions

After selecting a sender, the wizard suggests patterns from specific to broad:

```
Selected: info@notifications.natwest.com

Suggested patterns (broader patterns match more messages):
  1. info@notifications.natwest.com
     Exact match - 64 messages
  2. info@notifications.natwest.*
     All TLDs - 87 messages
  3. @notifications.natwest.com
     All from domain - 87 messages
  4. natwest
     All natwest domains - 102 messages

Select pattern [1-4] or Enter to use exact: 4
Using pattern: natwest
```

The wizard shows how many messages each pattern would match, helping you choose the right scope.

#### Option 2: To Address (Recipient)

Similar to "From address" but shows your email addresses (or aliases) that received the messages.

Useful for rules based on which of your email addresses was used (e.g., newsletter-specific addresses).

#### Option 3: Subject Line

Shows unique subject lines from your cache:

```
--- Subject Line Condition ------------------------------------

Loading subjects from cache...
Found 500 unique subjects.
```

After selecting a subject, the wizard suggests patterns:

```
Selected: Your Order #BRS-SRS-36558426 Has Shipped

Suggested patterns (broader patterns match more messages):
  1. Your Order #BRS-SRS-36558426 Has Shipped
     Exact match - 1 messages
  2. Your Order #BRS-SRS-* Has Shipped
     Without numbers - 15 messages
  3. Your Order
     First 2 words - 28 messages
  4. Order
     Keyword: Order - 67 messages

Select pattern [1-4] or Enter to use exact: 2
Using pattern: Your Order #BRS-SRS-* Has Shipped
```

#### Option 4: Other Header Field

For advanced users who want to filter on custom headers:

```
--- Custom Header Condition -----------------------------------

Header field name (e.g., 'list-id', 'x-mailer'): list-id
Value to match in list-id: reddit.com
```

Common headers:
- `list-id`: Mailing list identifier
- `x-mailer`: Email client used
- `reply-to`: Reply-to address
- `return-path`: Bounce handling address

#### Adding Multiple Conditions

After adding a condition:

```
  Condition added! (Total: 1)

What type of condition would you like to add?
  1. From address (sender)
  2. To address (recipient)
  3. Subject line
  4. Other header field
  5. Done adding conditions

Select option [1-5]: 5
```

Select **5** when done adding conditions.

#### Combining Multiple Conditions

If you added multiple conditions, choose how to combine them:

```
You have multiple conditions. How should they be combined?
  1. Match ANY condition (OR)
  2. Match ALL conditions (AND)

Select logic [1-2]: 1
  Logic: ANY condition can match (OR)
```

**ANY (OR):** Message matches if it matches any condition (most common)
**ALL (AND):** Message matches only if it matches all conditions (more restrictive)

---

### Step 3: Set Action

Choose what happens to matching messages:

```
--- Step 3: Set Action ----------------------------------------

What should happen when messages match this rule?
Currently only 'move' action is supported.

Target folder (e.g., 'Banking/NatWest' or 'Newsletters/Reddit'): Banking/NatWest

  Action: Move to 'Banking/NatWest'
```

Use forward slashes (`/`) to create nested folders. The IMAP server will create them if they don't exist.

---

### Step 4: Review and Save

The wizard shows a summary of your rule:

```
--- Step 4: Review and Save -----------------------------------

Rule summary:
  Name: Banking » NatWest
  Priority: 150
  Conditions: 1 condition(s)
    1. from contains 'natwest'
  Action: move to 'Banking/NatWest'

Save this rule? [Y/n] y

Rule saved successfully!
  Saved to /root/imapfilter/rules/99013_banking_natwest.json

You can now run your rules with: python -m core.cli run-all
```

The wizard generates a filename automatically based on your rule name and the next available ID number.

---

## Real-World Examples

### Example 1: Create "Newsletters » Reddit" Rule

**Goal:** Move all Reddit emails to `Newsletters/Reddit` folder.

**Steps:**

1. **Start wizard:**
   ```bash
   python rule_wizard.py
   ```

2. **Set basic info:**
   ```
   Rule name: Newsletters » Reddit
   Priority: [100] (press Enter for default)
   ```

3. **Add first condition (From address):**
   ```
   What type of condition: 1 (From address)
   ```

   In the filterable list, type `reddit` to filter, then select:
   ```
   noreply@redditmail.com (45 messages)
   ```

   Choose pattern:
   ```
   2. noreply@redditmail.* (All TLDs - 52 messages)
   ```

4. **Add second condition (another From address):**
   ```
   What type of condition: 1 (From address)
   ```

   In the list, type `reddit` and select:
   ```
   community@reddit.com (23 messages)
   ```

   Choose pattern:
   ```
   2. community@reddit.* (All TLDs - 28 messages)
   ```

5. **Done adding conditions:**
   ```
   What type of condition: 5 (Done)
   ```

6. **Set logic:**
   ```
   How to combine: 1 (ANY/OR)
   ```

7. **Set action:**
   ```
   Target folder: Newsletters/Reddit
   ```

8. **Save:**
   ```
   Save this rule? y
   ```

**Result:** Rule created at `rules/99013_newsletters_reddit.json`

---

### Example 2: Create "Banking » NatWest" Rule

**Goal:** Move all NatWest banking emails to one folder.

**Steps:**

1. **Start wizard and set name:**
   ```
   Rule name: Banking » NatWest
   Priority: 150
   ```

2. **Add condition (From address):**
   ```
   Type: 1 (From address)
   Filter list: nat
   Select: info@notifications.natwest.com (64)
   Pattern: 4 (natwest - All natwest domains - 87 messages)
   ```

3. **Done adding conditions:**
   ```
   Type: 5 (Done)
   ```

4. **Set action:**
   ```
   Target folder: Banking/NatWest
   ```

5. **Save:** Press `y`

**Result:** Single broad pattern matches all NatWest emails from any subdomain.

---

### Example 3: Multi-Condition Rule with ANY Logic

**Goal:** Move Amazon order confirmations and shipping notifications to `Shopping/Amazon` folder.

**Steps:**

1. **Set name:**
   ```
   Rule name: Shopping » Amazon Orders
   Priority: 120
   ```

2. **Add first condition (From address):**
   ```
   Type: 1
   Select: order-update@amazon.com
   Pattern: 2 (order-update@amazon.* - 134 messages)
   ```

3. **Add second condition (Subject):**
   ```
   Type: 3 (Subject)
   Select: Your Amazon.com order has shipped
   Pattern: 3 (Your Amazon.com order - 89 messages)
   ```

4. **Add third condition (Subject):**
   ```
   Type: 3
   Select: Your Amazon.com order has been delivered
   Pattern: 3 (Your Amazon.com order - includes both shipped and delivered)
   ```

5. **Done and set logic:**
   ```
   Type: 5 (Done)
   Logic: 1 (ANY)
   ```

6. **Set action:**
   ```
   Target folder: Shopping/Amazon
   ```

7. **Save:** Press `y`

**Result:** Matches messages from order-update@amazon.* OR with "Your Amazon.com order" in subject.

---

## Pattern Suggestions Explained

### What Are Pattern Suggestions?

When you select a sender or subject, the wizard analyzes it and suggests progressively broader patterns. Each suggestion shows an estimated message count based on your cache.

This helps you create rules that match the right scope:
- Too specific: Misses similar messages
- Too broad: Matches unrelated messages
- Just right: Catches what you want, nothing more

---

### Email Address Patterns

When you select an email like `noreply@amazon.com`, the wizard suggests:

#### 1. Exact Match
```
Pattern: noreply@amazon.com
Description: Exact match
Matches: 45 messages
```
**Use when:** You only want emails from this exact address.

**Example:** `support@example.com` when you don't want emails from `noreply@example.com`

#### 2. Wildcard TLD
```
Pattern: noreply@amazon.*
Description: All TLDs
Matches: 127 messages
```
**Use when:** Same sender from multiple country domains.

**Matches:**
- `noreply@amazon.com`
- `noreply@amazon.co.uk`
- `noreply@amazon.de`
- `noreply@amazon.fr`

**Example:** International companies that email from different TLDs based on your location.

#### 3. Domain Only
```
Pattern: @amazon.com
Description: All from domain
Matches: 203 messages
```
**Use when:** You want all emails from any address at this domain.

**Matches:**
- `noreply@amazon.com`
- `order-update@amazon.com`
- `customer-service@amazon.com`
- `marketplace-messages@amazon.com`

**Example:** Company with many sender addresses that you want to group together.

#### 4. Domain Base
```
Pattern: amazon
Description: All amazon domains
Matches: 298 messages
```
**Use when:** You want emails from all related domains and subdomains.

**Matches:**
- `noreply@amazon.com`
- `noreply@amazon.co.uk`
- `noreply@amazonses.com`
- `updates@marketplace.amazon.com`

**Example:** Large organizations with many domains and subdomains.

---

### Subject Line Patterns

When you select a subject like `Your Order #BRS-SRS-36558426 Has Shipped`, the wizard suggests:

#### 1. Exact Match
```
Pattern: Your Order #BRS-SRS-36558426 Has Shipped
Description: Exact match
Matches: 1 message
```
**Use when:** You only want this specific message (rarely useful for rules).

#### 2. Without Numbers
```
Pattern: Your Order #BRS-SRS-* Has Shipped
Description: Without numbers
Matches: 15 messages
```
**Use when:** The subject has order numbers, booking IDs, or tracking codes that change.

**Removes:**
- Numeric sequences: `12345` → `*`
- Alphanumeric codes: `BRS-SRS-36558426` → `*`
- Reference numbers: `REF-123-ABC` → `*`

**Example:** Order confirmations, booking receipts, tracking updates.

#### 3. First N Words
```
Pattern: Your Order
Description: First 2 words
Matches: 28 messages
```
**Use when:** Messages have similar beginnings but varying endings.

**Matches:**
- `Your Order #123 Has Shipped`
- `Your Order Has Been Delivered`
- `Your Order is Being Prepared`

**Example:** Status updates, notifications with varying content.

#### 4. Keywords
```
Pattern: Order
Description: Keyword: Order
Matches: 67 messages
```
**Use when:** You want all messages containing a specific keyword.

**Matches:**
- `Your Order #123 Has Shipped`
- `New Order from Customer`
- `Order Confirmation - Thank You`

**Example:** Broad category matching, catching all mentions of a topic.

---

## Features

### Real-Time Search/Filter in Lists

The filterable list selector supports instant search:

- **Type any letter** to filter items
- **Case-insensitive** substring matching
- **Live updates** as you type
- **Shows filtered count** vs total count

Example:
```
Filter: amaz

[showing 7 of 1,234]

>     1. noreply@amazon.com (245)
      2. order-update@amazon.co.uk (89)
      3. marketplace@amazon.de (34)
      ...
```

---

### Message Counts for Scope Decisions

Every pattern suggestion includes estimated message counts from your cache:

```
  2. noreply@amazon.* (All TLDs - 127 messages)
```

Use these counts to:
- **Verify the pattern** matches the right scope
- **Compare options** (exact vs broad)
- **Catch errors** (0 messages = pattern won't work)

---

### Smart Pattern Suggestions

The wizard analyzes your selections and suggests patterns intelligently:

**For email addresses:**
- Detects TLD variations (.com, .co.uk, .de)
- Identifies domain hierarchies (amazon.com vs amazonses.com)
- Suggests domain base for company groups

**For subjects:**
- Removes variable numbers and codes
- Extracts meaningful keywords
- Suggests first N words for consistent prefixes

---

### Dry-Run Preview Before Saving

Before saving, see exactly how many messages the rule will match:

```
Running dry-run preview...
This rule will match approximately 127 messages.

Save this rule? [Y/n]
```

This validates your rule against the actual cache data.

---

### Auto-Generated Filenames

The wizard creates filenames automatically:

**Format:** `{5-digit-id}_{slug}.json`

**Example:**
```
Rule name: Banking » NatWest
Filename: 99013_banking_natwest.json
```

The ID is auto-incremented from the highest existing rule number.

---

### Priority Management

Set rule priorities to control execution order:

- **Higher priority rules run first**
- **Default is 100**
- **Useful for layered filtering**

Example:
```
Priority 200: Important filters (urgent emails)
Priority 100: Standard filters (newsletters, shopping)
Priority 50:  Catch-all filters (everything else)
```

---

## Keyboard Controls

### Filterable List Selector

| Key | Action |
|-----|--------|
| **Letter/Number** | Add to filter (search) |
| **↑** or **k** | Move selection up |
| **↓** or **j** | Move selection down |
| **Page Up** | Jump up 10 items |
| **Page Down** | Jump down 10 items |
| **Home** | Jump to top |
| **End** | Jump to bottom |
| **Enter** | Select highlighted item |
| **Backspace** | Delete last filter character |
| **ESC** | Cancel and return to wizard |

### Text Prompts

| Input | Action |
|-------|--------|
| **Type text + Enter** | Submit answer |
| **Enter (empty)** | Use default value (if shown) |
| **Ctrl+C** | Exit wizard completely |

### Yes/No Prompts

| Input | Action |
|-------|--------|
| **y** or **yes** | Confirm yes |
| **n** or **no** | Confirm no |
| **Enter (empty)** | Use default (shown in brackets) |

---

## Common Tasks

### "How do I create a rule for all emails from a domain?"

1. Add a **From address** condition
2. Select any email from that domain
3. Choose pattern **3** or **4** (domain only or domain base)

**Example:** For `notifications@github.com`, choose `@github.com` to match all GitHub senders.

---

### "How do I match multiple senders?"

1. Add a **From address** condition for first sender
2. Select **1** when asked "Add another condition?"
3. Add another **From address** condition for second sender
4. Repeat as needed
5. When done, choose logic: **1 (ANY/OR)**

**Example:** Reddit sends from both `noreply@redditmail.com` and `community@reddit.com`. Add both as separate conditions with ANY logic.

---

### "How do I match emails with specific subject patterns?"

1. Add a **Subject line** condition
2. Select an example subject from the list
3. Choose the appropriate pattern:
   - **2** (Without numbers) for order confirmations
   - **3** (First N words) for status updates
   - **4** (Keywords) for broad topic matching

**Example:** For Amazon shipping notifications, select a subject like "Your Amazon.com order #123 has shipped" and choose pattern **2** (without numbers) to match all shipping notifications regardless of order number.

---

### "How do I combine conditions with AND vs OR?"

**Use OR (ANY) when:**
- Multiple senders for same category (Reddit from multiple addresses)
- Either this sender OR this subject pattern
- Catch-all rules (any of these conditions)

**Use AND (ALL) when:**
- Specific combinations (FROM someone AND TO specific address)
- Narrow filtering (FROM domain AND SUBJECT contains keyword)
- Precise targeting (all conditions must match)

**Example OR:** Emails FROM amazon.com OR amazon.co.uk (2 conditions, ANY logic)

**Example AND:** Emails FROM newsletters@company.com AND TO my-newsletter-alias@domain.com (2 conditions, ALL logic)

---

### "Can I edit a rule after creating it?"

**Yes!** Rules are JSON files in the `rules/` directory.

**Option 1: Edit manually**
```bash
nano rules/99013_banking_natwest.json
```

**Option 2: Use rule_manager.py**
```bash
python rule_manager.py
# Interactive console for managing rules
```

**Option 3: Delete and recreate**
```bash
rm rules/99013_banking_natwest.json
python rule_wizard.py
# Create the rule again
```

---

## Troubleshooting

### "Cache is empty"

**Error message:**
```
Cache is empty. Please build the cache first.
```

**Solution:**
```bash
./imapfilter_helper.py build-cache
```

**Cause:** The cache database exists but has no message headers. Build it to populate.

---

### "No cache found"

**Error message:**
```
Cache not found at: /root/imapfilter/data/cache.db
You need to build the cache before using the wizard.
Run: python -m core.cli build-cache
```

**Solution:**
```bash
python -m core.cli build-cache
```

**Cause:** The cache database file doesn't exist yet. Build it first.

---

### "Pattern not matching expected messages"

**Symptom:** Rule saved successfully but doesn't match the messages you expected when running `--dry-run`.

**Possible causes:**
1. **Cache is outdated** - Rebuild the cache if your mailbox changed
2. **Pattern is too specific** - Try a broader pattern
3. **Case sensitivity issue** - Rule uses "contains" which is case-insensitive, but your pattern might have extra characters

**Solution:**
```bash
# Rebuild cache to ensure it's current
./imapfilter_helper.py clear-cache
./imapfilter_helper.py build-cache

# Test the rule
./imapfilter_helper.py run-all --dry-run

# Check the logs for matching details
tail -f data/imapfilter-helper.log
```

---

### "Curses errors on some terminals"

**Error message:**
```
Could not use interactive selector: <curses error>
```

**Workaround:** The wizard falls back to manual entry when curses fails:
```
Enter sender address or pattern: noreply@amazon.com
```

**Cause:** Some terminals (especially SSH sessions, tmux, or minimal environments) don't support curses.

**Better solution:**
- Use a full terminal emulator (xterm, gnome-terminal, iTerm2)
- Set TERM environment variable:
  ```bash
  export TERM=xterm-256color
  python rule_wizard.py
  ```

---

### "No values found for header"

**Error message:**
```
No values found for header 'list-id' in cache.
Enter value manually (or press Enter to skip):
```

**Cause:** The custom header you specified doesn't exist in any cached messages.

**Solutions:**
1. **Verify header name** - Check that it's spelled correctly (case-insensitive)
2. **Try a different header** - Use "From" or "Subject" which always exist
3. **Enter manually** - If you know the value, type it when prompted
4. **Rebuild cache** with more folders:
   ```bash
   ./imapfilter_helper.py build-cache --all-folders
   ```

---

## Advanced Usage

### Using Regex Patterns (contains vs regex)

When selecting match type, you can choose:

**1. Contains (substring match):**
- Case-insensitive
- Simple pattern matching
- **Example:** `amazon` matches `amazon.com`, `amazonses.com`, `AMAZON`

**2. Regex (regular expression):**
- Full regex syntax support
- Case-sensitive by default
- **Example:** `^noreply@.*\.amazon\.(com|co\.uk)$`

**When to use regex:**
- Complex patterns (e.g., phone numbers, dates)
- Exact boundaries (start/end of string)
- Advanced logic (lookaheads, groups)

**Example regex patterns:**
```
^Order-\d{5}$              # Exactly "Order-" followed by 5 digits
.*@(amazon|aws|amazonses)  # Multiple domains
\[URGENT\].*               # Starts with [URGENT]
```

---

### Creating Rules with Complex Logic (Nested Conditions)

The wizard supports one level of nesting (all conditions combined with AND or OR). For more complex logic, edit the JSON file manually.

**Example:** (FROM amazon.com OR amazon.co.uk) AND (SUBJECT contains "order")

**Step 1: Create with wizard:**
```json
{
  "conditions": {
    "any": [
      {"header": "from", "contains": "amazon.com"},
      {"header": "from", "contains": "amazon.co.uk"}
    ]
  }
}
```

**Step 2: Edit manually to add nested condition:**
```json
{
  "conditions": {
    "all": [
      {
        "any": [
          {"header": "from", "contains": "amazon.com"},
          {"header": "from", "contains": "amazon.co.uk"}
        ]
      },
      {"header": "subject", "contains": "order"}
    ]
  }
}
```

---

### Dry-Run Preview Interpretation

The preview shows estimated matches based on your cache:

```
Running dry-run preview...
This rule will match approximately 127 messages.
```

**Interpreting the count:**

- **0 messages:** Pattern might be too specific or incorrect
- **1-10 messages:** Very specific rule, might miss similar messages
- **10-100 messages:** Good specific rule for a single sender/topic
- **100-1000 messages:** Broad rule, verify it's not too general
- **1000+ messages:** Very broad, might catch unintended messages

**Note:** This is based on your cache, not your entire mailbox. Actual matches may vary.

---

### Integration with rule_manager.py

The wizard creates standard rule files that work with `rule_manager.py`:

**View all rules:**
```bash
python rule_manager.py
# Interactive console
> list
```

**Edit a rule:**
```bash
python rule_manager.py
> edit 99013_banking_natwest
```

**Delete a rule:**
```bash
python rule_manager.py
> delete 99013_banking_natwest
```

---

## Tips & Tricks

### 1. Start with Broader Patterns to Test

When unsure, choose a broader pattern (option 2 or 3) and test with `--dry-run`:

```bash
./imapfilter_helper.py run-all --dry-run
```

Check the log to see what matched. If too broad, edit the rule to be more specific.

---

### 2. Use Message Count to Verify Scope

Pay attention to the message counts in pattern suggestions:

```
  1. exact@address.com (Exact match - 5 messages)
  2. exact@address.* (All TLDs - 5 messages)     # Same count = no other TLDs
  3. @address.com (All from domain - 87 messages) # Much broader!
```

If counts are the same for options 1 and 2, the broader pattern adds no value.

---

### 3. Name Rules Descriptively

Use consistent naming schemes:

**Good names:**
- `Banking » NatWest`
- `Banking » Chase Credit Card`
- `Newsletters » Reddit`
- `Shopping » Amazon Orders`
- `Social » LinkedIn`

**Bad names:**
- `Rule 1`
- `test`
- `new_rule`
- `asdf`

**Why:** Descriptive names help when debugging, reviewing logs, and managing many rules.

---

### 4. Use Lowercase or Special Characters in Folder Names

IMAP folder names are case-sensitive on some servers. For consistency:

**Recommended:**
- `Banking/NatWest` (PascalCase)
- `banking/natwest` (lowercase)
- `BANKING/NATWEST` (uppercase)

**Avoid mixing:**
- ~~`Banking/natwest`~~ (inconsistent case)
- ~~`banking/NatWest`~~ (inconsistent case)

**Special characters:**
- Use `/` for hierarchy: `Banking/Personal/NatWest`
- Use `»` or `-` for readability: `Banking » NatWest`

---

### 5. Check Dry-Run Preview Matches Expected Count

Before saving, verify the match count makes sense:

```
This rule will match approximately 127 messages.
```

**Sanity checks:**
- Does this align with how many emails you get from this sender?
- If you selected "amazon" as domain base, 127 might be too low (or cache is small)
- If you selected exact match, 127 might be too high (pattern is catching more than intended)

---

### 6. Test Rules Before Applying to Entire Mailbox

Always test with `--dry-run` first:

```bash
# Test without making changes
./imapfilter_helper.py run-all --dry-run

# Check what matched
tail -50 data/imapfilter-helper.log

# If looks good, run for real
./imapfilter_helper.py run-all
```

---

### 7. Build Cache from All Folders for Better Suggestions

If pattern suggestions seem incomplete, rebuild cache with all folders:

```bash
./imapfilter_helper.py clear-cache
./imapfilter_helper.py build-cache --all-folders
```

This gives the wizard more data to suggest better patterns.

---

### 8. Use Priority to Layer Filters

Set up a priority hierarchy:

**Priority 200:** Urgent/Important filters run first
```
200: Banking » Alerts (urgent account alerts)
200: Security » 2FA Codes
```

**Priority 100:** Standard filters
```
100: Banking » NatWest
100: Newsletters » Reddit
100: Shopping » Amazon
```

**Priority 50:** Catch-all filters
```
50: Newsletters » Unclassified
50: Promotions » General
```

This ensures high-priority rules get first pick of messages.

---

### 9. Group Related Rules with Similar Names

Use consistent prefixes for easier management:

```
Banking » NatWest
Banking » Chase
Banking » Alerts

Newsletters » Reddit
Newsletters » GitHub
Newsletters » Medium

Shopping » Amazon
Shopping » eBay
Shopping » Etsy
```

**Benefits:**
- Easy to see all rules in a category
- Filename sorting groups them together
- Log filtering becomes simpler

---

### 10. Keep a Backup of Your Rules

Rules are in the `rules/` directory. Back them up:

```bash
# Git (recommended)
git add rules/
git commit -m "Add Banking » NatWest rule"
git push

# Or manual backup
tar -czf rules-backup-$(date +%Y%m%d).tar.gz rules/
```

---

## Summary

The IMAPFilter Rule Wizard makes creating email filter rules fast and accurate by:

1. **Showing real data** from your mailbox cache
2. **Suggesting patterns** automatically with message counts
3. **Validating rules** before saving
4. **Generating filenames** automatically

**Workflow:**
1. Build cache: `./imapfilter_helper.py build-cache`
2. Run wizard: `python rule_wizard.py`
3. Create rules interactively
4. Test: `./imapfilter_helper.py run-all --dry-run`
5. Apply: `./imapfilter_helper.py run-all`

**File locations:**
- Wizard: `/root/imapfilter/rule_wizard.py`
- Core: `/root/imapfilter/core/tools/rule_wizard_core.py`
- Rules: `/root/imapfilter/rules/*.json`
- Cache: `/root/imapfilter/data/cache.db`

**Quick reference:**
```bash
# Setup
./imapfilter_helper.py build-cache

# Create rules
python rule_wizard.py

# Test
./imapfilter_helper.py run-all --dry-run

# Apply
./imapfilter_helper.py run-all

# Manage
python rule_manager.py
```

---

**Questions or issues?** See the main README.md or check existing documentation in the repo.

**Word count:** ~5,800 words
**Status:** ✅ Ready for use
**Last updated:** 2025-11-30
