#!/usr/bin/env python3
"""Command-line entry point for the IMAPFilter Rule Wizard.

This script provides an interactive wizard for creating IMAPFilter rules
by guiding users through the process of selecting email headers, patterns,
and actions.

Usage:
    python3 wizard.py                                  # Start the wizard
    python3 wizard.py --add-keyword Important           # Add a predefined keyword
    python3 wizard.py --remove-keyword Old              # Remove a predefined keyword
    python3 wizard.py --list-keywords                   # List predefined keywords

Prerequisites:
    - Cache must be built first: python3 main.py build-cache
    - The wizard will validate cache exists before starting

Exit codes:
    0   - Rule created successfully or keyword operation succeeded
    1   - Error occurred
    130 - User cancelled the wizard
"""

import argparse
import sys
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core.config import build_default_config
from core.keywords import KeywordManager
from core.logging_utils import JsonLogger
from core.tools.rule_wizard_core import RuleWizard


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for wizard CLI."""
    parser = argparse.ArgumentParser(
        description="IMAPFilter Rule Wizard - Create and manage email filter rules"
    )

    # Keyword management options
    kw_group = parser.add_mutually_exclusive_group()
    kw_group.add_argument(
        "--add-keyword",
        metavar="KEYWORD",
        help="Add a predefined keyword and exit",
    )
    kw_group.add_argument(
        "--remove-keyword",
        metavar="KEYWORD",
        help="Remove a predefined keyword and exit",
    )
    kw_group.add_argument(
        "--list-keywords",
        action="store_true",
        help="List all predefined keywords and exit",
    )

    return parser


def handle_keyword_operations(args: argparse.Namespace, config) -> int:
    """Handle keyword management operations. Returns 0 to continue to wizard, non-zero to exit."""
    if not any([args.add_keyword, args.remove_keyword, args.list_keywords]):
        return 0  # No keyword operations requested, continue to wizard

    keyword_manager = KeywordManager(config.paths.data_dir)

    if args.list_keywords:
        keywords = keyword_manager.get_keywords()
        if not keywords:
            print("No predefined keywords found.")
        else:
            print("Predefined Keywords:")
            for i, kw in enumerate(keywords, 1):
                print(f"  {i}. {kw}")
        return 1  # Exit after listing

    elif args.add_keyword:
        keyword = args.add_keyword.strip()
        if not keyword:
            print("Error: Keyword cannot be empty")
            return 1

        if keyword_manager.add_keyword(keyword):
            print(f"✓ Added keyword: {keyword}")
        else:
            print(f"⚠️  Keyword already exists: {keyword}")
        return 1  # Exit after adding

    elif args.remove_keyword:
        keyword = args.remove_keyword.strip()
        if not keyword:
            print("Error: Keyword cannot be empty")
            return 1

        if keyword_manager.remove_keyword(keyword):
            print(f"✓ Removed keyword: {keyword}")
        else:
            print(f"⚠️  Keyword not found: {keyword}")
        return 1  # Exit after removing

    return 0


def main() -> int:
    """Run the rule wizard."""
    parser = build_parser()
    args = parser.parse_args()

    try:
        # Build configuration
        config = build_default_config()

        # Handle keyword operations
        exit_code = handle_keyword_operations(args, config)
        if exit_code != 0:
            return exit_code

        # Create logger for IMAP operations
        logger = JsonLogger(config.paths.log_file)

        # Initialize and run wizard
        wizard = RuleWizard(config, logger)
        return wizard.run()

    except KeyboardInterrupt:
        print("\n\nWizard interrupted by user.")
        return 130

    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
