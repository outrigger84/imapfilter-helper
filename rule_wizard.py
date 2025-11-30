#!/usr/bin/env python3
"""
Cache-assisted guided rule creation wizard.

Analyzes cached email headers and presents common senders,
recipients, and patterns with message counts to help create
accurate filter rules quickly.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Optional

from core.config import build_default_config, AppConfig
from core.tools.rule_wizard_core import (
    CacheQueryEngine,
    EmailPatternExtractor,
    SubjectPatternExtractor,
    RuleBuilder,
    FilterableListSelector,
    format_count,
    save_rule,
)


def clear_screen() -> None:
    """Clear the terminal screen for a clean display."""
    print("\033[2J\033[H", end="")


def print_banner() -> None:
    """Display the wizard banner and welcome message."""
    clear_screen()
    print("=" * 64)
    print("   IMAPFilter Rule Wizard")
    print("=" * 64)
    print()


def print_section_header(title: str) -> None:
    """Print a section header with consistent formatting."""
    print()
    print(f"--- {title} " + "-" * (60 - len(title)))
    print()


def prompt(message: str, *, allow_empty: bool = False, default: str = "") -> str:
    """Prompt the user for text input.

    Args:
        message: The prompt message to display
        allow_empty: Whether to allow empty responses
        default: Default value if user enters nothing (only used if allow_empty=True)

    Returns:
        User's input string (stripped of whitespace)
    """
    while True:
        if default and allow_empty:
            display_message = f"{message} [{default}]: "
        else:
            display_message = f"{message}: "

        value = input(display_message).strip()

        if not value and allow_empty:
            return default if default else ""

        if value:
            return value

        print("  Please enter a value or press CTRL+C to cancel.")


def prompt_int(message: str, *, default: int = 100, min_val: int = 1) -> int:
    """Prompt for an integer value.

    Args:
        message: The prompt message to display
        default: Default value if user enters nothing
        min_val: Minimum acceptable value

    Returns:
        User's integer input
    """
    while True:
        raw = input(f"{message} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
            if value < min_val:
                print(f"  Please enter a number >= {min_val}")
                continue
            return value
        except ValueError:
            print("  Please enter a valid whole number.")


def confirm(message: str, *, default: bool = True) -> bool:
    """Prompt for yes/no confirmation.

    Args:
        message: The question to ask
        default: Default value if user presses Enter

    Returns:
        True for yes, False for no
    """
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        choice = input(f"{message} {suffix} ").strip().lower()
        if not choice:
            return default
        if choice in {"y", "yes"}:
            return True
        if choice in {"n", "no"}:
            return False
        print("  Please respond with 'y' or 'n'.")


class RuleWizard:
    """Interactive wizard for creating IMAPFilter rules with cache assistance.

    This wizard guides users through creating email filter rules by:
    1. Analyzing the email cache for common patterns
    2. Presenting statistics and suggestions
    3. Building rule conditions interactively
    4. Validating and saving the final rule
    """

    def __init__(self, config: AppConfig):
        """Initialize the wizard with application configuration.

        Args:
            config: Application configuration containing paths and settings
        """
        self.config = config
        self.cache_path = config.paths.db_file
        self.rules_dir = config.paths.rules_dir
        self.cache_engine: Optional[CacheQueryEngine] = None
        self.builder = RuleBuilder()

    def run(self) -> int:
        """Run the interactive wizard.

        Returns:
            Exit code (0 for success, 1 for error)
        """
        print_banner()

        # Check cache availability
        if not self._check_cache():
            return 1

        # Initialize cache engine
        try:
            self.cache_engine = CacheQueryEngine(self.cache_path)
            print("Cache loaded successfully!")
        except Exception as e:
            print(f"Failed to open cache: {e}")
            return 1

        try:
            # Step 1: Rule name and priority
            if not self._step_basic_info():
                return 0

            # Step 2: Add conditions
            if not self._step_add_conditions():
                return 0

            # Step 3: Set action
            if not self._step_set_action():
                return 0

            # Step 4: Review and save
            return self._step_review_and_save()

        finally:
            if self.cache_engine:
                self.cache_engine.close()

    def _check_cache(self) -> bool:
        """Check if cache database exists and has data.

        Returns:
            True if cache is available, False otherwise
        """
        print_section_header("Checking Cache")

        if not self.cache_path.exists():
            print(f"Cache not found at: {self.cache_path}")
            print()
            print("You need to build the cache before using the wizard.")
            print("Run: python -m core.cli build-cache")
            return False

        print(f"Found cache at: {self.cache_path}")

        # Try to get message count
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.cache_path))
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM headers")
            count = cursor.fetchone()[0]
            conn.close()

            if count == 0:
                print("Cache is empty. Please build the cache first.")
                return False

            print(f"Found {format_count(count)} cached messages")
            return True
        except Exception as e:
            print(f"Error reading cache: {e}")
            return False

    def _step_basic_info(self) -> bool:
        """Step 1: Collect rule name and priority.

        Returns:
            True to continue, False to cancel
        """
        print_section_header("Step 1: Basic Information")

        name = prompt("Rule name (e.g., 'Banking - NatWest')")
        if not name:
            return False

        priority = prompt_int("Rule priority", default=100, min_val=1)

        self.builder.set_name(name)
        self.builder.set_priority(priority)

        print()
        print(f"  Name: {name}")
        print(f"  Priority: {priority}")

        return True

    def _step_add_conditions(self) -> bool:
        """Step 2: Add rule conditions interactively.

        Returns:
            True to continue, False to cancel
        """
        print_section_header("Step 2: Add Conditions")

        print("Add conditions to match messages. You can add multiple conditions.")
        print()

        while True:
            # Ask what kind of condition to add
            print("What type of condition would you like to add?")
            print("  1. From address (sender)")
            print("  2. To address (recipient)")
            print("  3. Subject line")
            print("  4. Other header field")
            print("  5. Done adding conditions")
            print()

            choice = input("Select option [1-5]: ").strip()

            if choice == "5":
                # Check if we have at least one condition
                if not self.builder.conditions:
                    print("  You must add at least one condition.")
                    print()
                    continue
                break

            if choice == "1":
                if not self._add_from_condition():
                    return False
            elif choice == "2":
                if not self._add_to_condition():
                    return False
            elif choice == "3":
                if not self._add_subject_condition():
                    return False
            elif choice == "4":
                if not self._add_custom_header_condition():
                    return False
            else:
                print("  Invalid choice. Please enter 1-5.")
                print()
                continue

            print()
            print(f"  Condition added! (Total: {len(self.builder.conditions)})")
            print()

        # If multiple conditions, ask for logic
        if len(self.builder.conditions) > 1:
            print()
            print("You have multiple conditions. How should they be combined?")
            print("  1. Match ANY condition (OR)")
            print("  2. Match ALL conditions (AND)")
            print()

            logic_choice = input("Select logic [1-2]: ").strip()
            if logic_choice == "2":
                self.builder.set_logic("all")
                print("  Logic: ALL conditions must match (AND)")
            else:
                self.builder.set_logic("any")
                print("  Logic: ANY condition can match (OR)")

        return True

    def _add_from_condition(self) -> bool:
        """Add a From address condition with cache-assisted suggestions."""
        print_section_header("From Address Condition")

        if not self.cache_engine:
            return False

        print("Loading top senders from cache...")
        senders = self.cache_engine.extract_unique_from_addresses(limit=1000)

        if not senders:
            print("No senders found in cache.")
            return True

        print(f"Found {len(senders)} unique senders.")
        print()

        # Use filterable selector
        try:
            import curses
            selector = FilterableListSelector(senders, "Select Sender")
            selected = curses.wrapper(selector.run)

            if not selected:
                print("Selection cancelled.")
                return True
        except Exception as e:
            print(f"Could not use interactive selector: {e}")
            # Fallback to manual entry
            selected = prompt("Enter sender address or pattern")
            if not selected:
                return True

        print()
        print(f"Selected: {selected}")

        # Suggest patterns
        extractor = EmailPatternExtractor()
        patterns = extractor.suggest_patterns(selected, self.cache_engine)

        if len(patterns) > 1:
            print()
            print("Suggested patterns (broader patterns match more messages):")
            for idx, (pattern, desc, count) in enumerate(patterns, 1):
                print(f"  {idx}. {pattern}")
                print(f"     {desc} - {format_count(count)} messages")
            print()

            pattern_choice = input(f"Select pattern [1-{len(patterns)}] or Enter to use exact: ").strip()
            if pattern_choice.isdigit():
                idx = int(pattern_choice) - 1
                if 0 <= idx < len(patterns):
                    selected = patterns[idx][0]
                    print(f"Using pattern: {selected}")

        self.builder.add_condition("from", "contains", selected)
        return True

    def _add_to_condition(self) -> bool:
        """Add a To address condition with cache-assisted suggestions."""
        print_section_header("To Address Condition")

        if not self.cache_engine:
            return False

        print("Loading top recipients from cache...")
        recipients = self.cache_engine.extract_unique_to_addresses(limit=1000)

        if not recipients:
            print("No recipients found in cache.")
            return True

        print(f"Found {len(recipients)} unique recipients.")
        print()

        # Use filterable selector
        try:
            import curses
            selector = FilterableListSelector(recipients, "Select Recipient")
            selected = curses.wrapper(selector.run)

            if not selected:
                print("Selection cancelled.")
                return True
        except Exception as e:
            print(f"Could not use interactive selector: {e}")
            selected = prompt("Enter recipient address or pattern")
            if not selected:
                return True

        print()
        print(f"Selected: {selected}")

        self.builder.add_condition("to", "contains", selected)
        return True

    def _add_subject_condition(self) -> bool:
        """Add a Subject condition with cache-assisted suggestions."""
        print_section_header("Subject Line Condition")

        if not self.cache_engine:
            return False

        print("Loading subjects from cache...")
        subjects = self.cache_engine.extract_unique_subjects(limit=500)

        if not subjects:
            print("No subjects found in cache.")
            return True

        print(f"Found {len(subjects)} unique subjects.")
        print()

        # Use filterable selector
        try:
            import curses
            selector = FilterableListSelector(subjects, "Select Subject")
            selected = curses.wrapper(selector.run)

            if not selected:
                print("Selection cancelled.")
                return True
        except Exception as e:
            print(f"Could not use interactive selector: {e}")
            selected = prompt("Enter subject text or pattern")
            if not selected:
                return True

        print()
        print(f"Selected: {selected}")

        # Suggest patterns
        extractor = SubjectPatternExtractor()
        patterns = extractor.suggest_patterns(selected, self.cache_engine)

        if len(patterns) > 1:
            print()
            print("Suggested patterns (broader patterns match more messages):")
            for idx, (pattern, desc, count) in enumerate(patterns, 1):
                print(f"  {idx}. {pattern}")
                print(f"     {desc} - {format_count(count)} messages")
            print()

            pattern_choice = input(f"Select pattern [1-{len(patterns)}] or Enter to use exact: ").strip()
            if pattern_choice.isdigit():
                idx = int(pattern_choice) - 1
                if 0 <= idx < len(patterns):
                    selected = patterns[idx][0]
                    print(f"Using pattern: {selected}")

        self.builder.add_condition("subject", "contains", selected)
        return True

    def _add_custom_header_condition(self) -> bool:
        """Add a custom header field condition."""
        print_section_header("Custom Header Condition")

        header = prompt("Header field name (e.g., 'list-id', 'x-mailer')")
        if not header:
            return True

        value = prompt(f"Value to match in {header}")
        if not value:
            return True

        self.builder.add_condition(header, "contains", value)
        return True

    def _step_set_action(self) -> bool:
        """Step 3: Set the rule action.

        Returns:
            True to continue, False to cancel
        """
        print_section_header("Step 3: Set Action")

        print("What should happen when messages match this rule?")
        print("Currently only 'move' action is supported.")
        print()

        target = prompt("Target folder (e.g., 'Banking/NatWest' or 'Newsletters/Reddit')")
        if not target:
            return False

        self.builder.set_action("move", target)

        print()
        print(f"  Action: Move to '{target}'")

        return True

    def _step_review_and_save(self) -> int:
        """Step 4: Review the rule and save it.

        Returns:
            Exit code (0 for success, 1 for error)
        """
        print_section_header("Step 4: Review and Save")

        # Validate the rule
        valid, error = self.builder.validate()
        if not valid:
            print(f"Rule validation failed: {error}")
            return 1

        # Generate and display the rule
        try:
            rule = self.builder.generate_rule()
        except ValueError as e:
            print(f"Error generating rule: {e}")
            return 1

        print("Rule summary:")
        print(f"  Name: {rule['name']}")
        print(f"  Priority: {rule['priority']}")
        print(f"  Conditions: {len(self.builder.conditions)} condition(s)")
        for idx, cond in enumerate(self.builder.conditions, 1):
            header = cond.get('header', '?')
            match_type = 'contains' if 'contains' in cond else 'regex'
            value = cond.get(match_type, '')
            print(f"    {idx}. {header} {match_type} '{value}'")

        logic = rule['conditions'].get('any') and 'any' or 'all'
        if len(self.builder.conditions) > 1:
            print(f"  Logic: {logic.upper()}")

        action = rule['action']
        print(f"  Action: {action['type']} to '{action['target']}'")
        print()

        # Confirm save
        if not confirm("Save this rule?", default=True):
            print("Rule discarded.")
            return 0

        # Save the rule
        success, message = save_rule(rule, self.rules_dir)

        if success:
            print()
            print(f"Rule saved successfully!")
            print(f"  {message}")
            print()
            print("You can now run your rules with: python -m core.cli run-all")
            return 0
        else:
            print()
            print(f"Failed to save rule: {message}")
            return 1


def main() -> int:
    """Main entry point for the rule wizard.

    Returns:
        Exit code (0 for success, 1 for error, 130 for user cancellation)
    """
    try:
        # Load configuration
        config = build_default_config()

        # Initialize and run wizard
        wizard = RuleWizard(config)
        return wizard.run()

    except KeyboardInterrupt:
        print()
        print()
        print("Wizard cancelled by user.")
        return 130

    except Exception as exc:
        print()
        print(f"Unexpected error: {exc}")
        print()
        print("Traceback:")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
