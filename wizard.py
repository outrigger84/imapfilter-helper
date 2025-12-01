#!/usr/bin/env python3
"""Command-line entry point for the IMAPFilter Rule Wizard.

This script provides an interactive wizard for creating IMAPFilter rules
by guiding users through the process of selecting email headers, patterns,
and actions.

Usage:
    python3 wizard.py

Prerequisites:
    - Cache must be built first: python3 main.py build-cache
    - The wizard will validate cache exists before starting

Exit codes:
    0   - Rule created successfully
    1   - Error occurred
    130 - User cancelled the wizard
"""

import sys
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core.config import build_default_config
from core.logging_utils import JsonLogger
from core.tools.rule_wizard_core import RuleWizard


def main() -> int:
    """Run the rule wizard."""
    try:
        # Build configuration
        config = build_default_config()

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
