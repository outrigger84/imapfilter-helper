"""Shared UI components for consistent user interaction.

This module provides common UI functions used by both the rule wizard
and rule manager to ensure consistent user experience and reduce code duplication.
"""
from typing import Optional


def prompt_yes_no(
    question: str,
    default: Optional[bool] = None
) -> bool:
    """Prompt user for yes/no answer with optional default.

    Args:
        question: Question to ask (without punctuation or (y/n) suffix)
        default: Default answer if user just presses Enter
                 True = default yes (Y/n)
                 False = default no (y/N)
                 None = no default (y/n)

    Returns:
        True for yes, False for no

    Examples:
        >>> # With default yes
        >>> prompt_yes_no("Continue?", default=True)
        Continue? (Y/n): ▌
        >>> # With default no
        >>> prompt_yes_no("Delete file?", default=False)
        Delete file? (y/N): ▌
        >>> # No default
        >>> prompt_yes_no("Proceed?")
        Proceed? (y/n): ▌
    """
    if default is True:
        prompt_text = f"{question} (Y/n): "
    elif default is False:
        prompt_text = f"{question} (y/N): "
    else:
        prompt_text = f"{question} (y/n): "

    while True:
        response = input(prompt_text).strip().lower()

        # Handle default
        if not response:
            if default is not None:
                return default
            # If no default and empty, ask again
            continue

        # Handle explicit yes/no
        if response in ('y', 'yes'):
            return True
        elif response in ('n', 'no'):
            return False
        else:
            print("⚠️  Please enter 'y' or 'n'.")


def format_count(count: int) -> str:
    """Format number with thousand separators for readability.

    Args:
        count: Number to format

    Returns:
        Formatted string like "1,234"

    Examples:
        >>> format_count(1234567)
        '1,234,567'
        >>> format_count(100)
        '100'
    """
    return f"{count:,}"
