#!/usr/bin/env python3
"""Command-line entry point for the IMAPFilter Rule Wizard.

This script provides an interactive wizard for creating IMAPFilter rules
by guiding users through the process of selecting email headers, patterns,
and actions.

Usage:
    python3 wizard.py                                  # Start the wizard
    python3 wizard.py --cache-file partial.db          # Use specific cache file
    python3 wizard.py --cache-file /tmp/test.db        # Use absolute path
    python3 wizard.py --add-keyword Important           # Add a predefined keyword
    python3 wizard.py --remove-keyword Old              # Remove a predefined keyword
    python3 wizard.py --list-keywords                   # List predefined keywords

Prerequisites:
    - Cache must be built first: python3 main.py build-cache
    - For custom cache: python3 main.py build-cache --limit 100
      (creates a small cache you can use while building the full one)

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


def validate_cache_file(cache_path: Path) -> Path:
    """Validate cache file exists and is a valid SQLite database.

    Args:
        cache_path: Path to cache database file

    Returns:
        Resolved absolute path

    Raises:
        SystemExit: If validation fails
    """
    import sqlite3

    resolved = cache_path.resolve()

    # Check file exists
    if not resolved.exists():
        print(f"Error: Cache file not found: {resolved}")
        print("Build a cache first: python3 main.py build-cache --limit 100")
        sys.exit(1)

    # Check it's a file
    if not resolved.is_file():
        print(f"Error: Cache path is not a file: {resolved}")
        sys.exit(1)

    # Validate SQLite database with required schema
    try:
        conn = sqlite3.connect(str(resolved))
        cursor = conn.cursor()

        # Check for required tables
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name IN ('headers', 'folders', 'actions')"
        )
        tables = {row[0] for row in cursor.fetchall()}

        if 'headers' not in tables:
            print(f"Error: Not a valid cache database (missing 'headers' table): {resolved}")
            conn.close()
            sys.exit(1)

        conn.close()
    except sqlite3.Error as e:
        print(f"Error: Cannot open cache database: {e}")
        sys.exit(1)

    return resolved


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

    # Cache management options
    cache_group = parser.add_argument_group("cache management")
    cache_group.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear wizard cache and exit",
    )
    cache_group.add_argument(
        "--cache-status",
        action="store_true",
        help="Display cache status and exit",
    )
    cache_group.add_argument(
        "--cache-ttl",
        type=int,
        metavar="HOURS",
        help="Override cache TTL in hours (default: 6)",
    )
    cache_group.add_argument(
        "--cache-file",
        type=Path,
        metavar="PATH",
        help="Use specified cache database instead of default (data/cache.db). "
             "Useful for working from a partial cache while building the full cache.",
    )

    # Wizard options
    wizard_group = parser.add_argument_group("wizard options")
    wizard_group.add_argument(
        "--fast-mode",
        action="store_true",
        help="Skip pattern effectiveness checking when suggesting criteria. "
             "Faster rule creation (5-10 seconds per criteria) at the cost of potentially "
             "overly broad patterns. Default: False (patterns are checked)",
    )
    wizard_group.add_argument(
        "--vacuum",
        action="store_true",
        help="Defragment the cache database and exit. "
             "Reduces file size and improves query performance. Requires 2-5 minutes.",
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


def handle_cache_operations(args: argparse.Namespace, config, logger) -> int:
    """Handle cache management operations. Returns 0 to continue to wizard, non-zero to exit."""
    if not any([args.clear_cache, args.cache_status, args.vacuum]):
        return 0  # No cache operations requested, continue to wizard

    # Initialize wizard instance for cache operations
    wizard = RuleWizard(config, logger)

    if args.clear_cache:
        wizard.invalidate_cache()
        return 1  # Exit after clearing cache

    elif args.cache_status:
        wizard.show_cache_status()
        return 1  # Exit after showing status

    elif args.vacuum:
        # Vacuum the cache database
        import sqlite3
        try:
            db_path = config.paths.db_file
            print(f"🔧 Defragmenting cache database: {db_path}")
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            # Get size before
            cursor.execute("SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()")
            size_before = cursor.fetchone()[0]

            print("   Running VACUUM...")
            cursor.execute("VACUUM")

            # Get size after
            cursor.execute("SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()")
            size_after = cursor.fetchone()[0]

            conn.close()

            size_reduction_mb = (size_before - size_after) / (1024 * 1024)
            if size_reduction_mb > 0:
                print("✓ Defragmentation complete")
                print(f"  Size reduced by {size_reduction_mb:.1f} MB")
            else:
                print("✓ Database already optimized")
            return 1  # Exit after vacuuming
        except Exception as e:
            print(f"Error: Could not vacuum database: {e}")
            return 1

    return 0


def main() -> int:
    """Run the rule wizard."""
    parser = build_parser()
    args = parser.parse_args()

    try:
        # Build configuration
        config = build_default_config()

        # Override cache path if specified
        if args.cache_file:
            validated_path = validate_cache_file(args.cache_file)
            config.paths.db_file = validated_path
            print(f"Using cache file: {validated_path}")

        # Create logger for IMAP operations
        logger = JsonLogger(config.paths.log_file)

        # Handle keyword operations
        exit_code = handle_keyword_operations(args, config)
        if exit_code != 0:
            return exit_code

        # Handle cache operations
        exit_code = handle_cache_operations(args, config, logger)
        if exit_code != 0:
            return exit_code

        # Initialize wizard
        wizard = RuleWizard(config, logger)

        # Set cache TTL override if provided
        if args.cache_ttl:
            wizard.cache_ttl_override = args.cache_ttl

        # Set fast mode flag if provided
        if args.fast_mode:
            wizard.fast_mode = True

        # Run wizard
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
