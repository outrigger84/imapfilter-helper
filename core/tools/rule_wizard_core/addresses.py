"""Email address parsing, display-name extraction, and address grouping."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from core.tools.rule_wizard_core.selector import ExpandableItem


@dataclass
class DisplayNameVariation:
    """Represents a single display name variation for an email address.

    This tracks a specific variation of how an email address appears in messages,
    including the full address with display name and the count of messages using it.

    Attributes:
        full_address: The complete email address string with display name
                     (e.g., "ClearScore <marketing@clearscore.com>")
        display_name: The extracted display name portion (e.g., "ClearScore")
        count: Number of messages with this exact variation
    """
    full_address: str
    display_name: str
    count: int


@dataclass
class EmailGroup:
    """Consolidates a single email address with all its display name variations.

    This groups together an email address and all the different ways it appears
    in messages (with different display names), tracking the total count and
    individual variation counts.

    Attributes:
        email: The normalized email address (e.g., "marketing@clearscore.com")
        total_count: Total number of messages from this email (sum of all variations)
        variations: List of DisplayNameVariation objects for this email
    """
    email: str
    total_count: int
    variations: List[DisplayNameVariation]

    @property
    def variation_count(self) -> int:
        """Get the number of different display name variations for this email.

        Returns:
            Number of distinct variations (len of variations list)
        """
        return len(self.variations)

    @property
    def has_variations(self) -> bool:
        """Check if this email has multiple display name variations.

        Returns:
            True if more than one variation exists, False otherwise
        """
        return len(self.variations) > 1


def extract_email_address(addr: str) -> str:
    """Extract the actual email address from various display name formats.

    Handles formats like:
    - email@domain.com → email@domain.com
    - <email@domain.com> → email@domain.com
    - Name <email@domain.com> → email@domain.com
    - "Name" <email@domain.com> → email@domain.com

    Args:
        addr: Email address string, possibly with display name

    Returns:
        The email address without display name or angle brackets
    """
    addr = addr.strip()
    if '<' in addr and '>' in addr:
        # Extract content between angle brackets
        start = addr.index('<')
        end = addr.index('>', start)
        return addr[start + 1:end].strip()
    return addr


def _extract_display_name(addr: str) -> str:
    """Extract the display name from an email address string.

    Handles various formats:
    - 'email@domain.com' → '' (no display name)
    - '<email@domain.com>' → '' (no display name)
    - 'Name <email@domain.com>' → 'Name'
    - '"Name" <email@domain.com>' → 'Name' (quotes removed)
    - 'First Last <email@domain.com>' → 'First Last'

    Args:
        addr: Email address string, possibly with display name

    Returns:
        The display name string, or empty string if no display name found
    """
    addr = addr.strip()
    if '<' not in addr or '>' not in addr:
        # No angle brackets means no display name
        return ''

    # Get the part before the angle brackets
    display_part = addr[:addr.index('<')].strip()

    # Remove surrounding quotes if present
    if display_part.startswith('"') and display_part.endswith('"'):
        display_part = display_part[1:-1].strip()

    return display_part


def create_expandable_email_items(
    email_groups: List[EmailGroup]
) -> List[ExpandableItem]:
    """Convert EmailGroup objects to ExpandableItem format for selector.

    Creates a hierarchical structure where:
    - Parent: The consolidated email address (optionally marked with variation count)
    - Children: Individual display name variations with their counts

    Args:
        email_groups: List of consolidated email groups with variations

    Returns:
        List of ExpandableItem objects ready for FilterableListSelector

    Example:
        Input: [EmailGroup("m@c.com", 150, [
                    DisplayNameVariation("Name1 <m@c.com>", "Name1", 100),
                    DisplayNameVariation("Name2 <m@c.com>", "Name2", 50)
                ])]
        Output: [ExpandableItem(
                    label="m@c.com [2 display names]",
                    count=150,
                    is_expandable=True,
                    children=[
                        ExpandableItem(label="Name1", count=100, indent_level=1),
                        ExpandableItem(label="Name2", count=50, indent_level=1)
                    ]
                )]
    """
    items = []

    for group in email_groups:
        # Create parent item
        if group.has_variations:
            label = f"{group.email} [{group.variation_count} display names]"
        else:
            label = group.email

        parent = ExpandableItem(
            label=label,
            count=group.total_count,
            is_expandable=group.has_variations,
            is_expanded=False,
            data=group,
            indent_level=0
        )

        # Create child items for variations (only if multiple)
        if group.has_variations:
            children = []
            for variation in group.variations:
                # Display format: display name if available, otherwise full address
                if variation.display_name:
                    child_label = variation.display_name
                else:
                    child_label = variation.full_address

                child = ExpandableItem(
                    label=child_label,
                    count=variation.count,
                    is_expandable=False,
                    is_expanded=False,
                    data=variation,
                    indent_level=1
                )
                children.append(child)

            parent.children = children

        items.append(parent)

    return items


def compute_domain_counts(addresses: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
    """Aggregate message counts grouped by domain.

    Args:
        addresses: List of (email_address, count) tuples from cache

    Returns:
        List of (domain, total_count) sorted by count descending

    Example:
        >>> addresses = [
        ...     ('noreply@amazon.com', 234),
        ...     ('orders@amazon.com', 45),
        ...     ('support@bank.com', 156),
        ... ]
        >>> compute_domain_counts(addresses)
        [('amazon.com', 279), ('bank.com', 156)]
    """
    from collections import defaultdict

    domain_counts = defaultdict(int)
    for addr, count in addresses:
        email = extract_email_address(addr)
        if '@' in email:
            domain = email.rsplit('@', 1)[1].strip().lower()
            domain_counts[domain] += count

    return sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)


def get_emails_for_domain(
    addresses: List[Tuple[str, int]], domain: str
) -> List[Tuple[str, int]]:
    """Get all email addresses from a specific domain.

    Args:
        addresses: List of (email_address, count) tuples
        domain: Domain to filter by (e.g., 'amazon.com')

    Returns:
        List of (email_address, count) for that domain, sorted by count descending

    Example:
        >>> addresses = [
        ...     ('noreply@amazon.com', 234),
        ...     ('support@bank.com', 156),
        ...     ('orders@amazon.com', 45),
        ... ]
        >>> get_emails_for_domain(addresses, 'amazon.com')
        [('noreply@amazon.com', 234), ('orders@amazon.com', 45)]
    """
    domain_lower = domain.lower().strip()
    filtered = [
        (addr, count)
        for addr, count in addresses
        if '@' in (email := extract_email_address(addr)) and email.rsplit('@', 1)[1].lower() == domain_lower
    ]
    # Sort by count descending
    return sorted(filtered, key=lambda x: x[1], reverse=True)


def consolidate_email_addresses(
    addresses: List[Tuple[str, int]], preserve_variations: bool = False
) -> List:
    """Consolidate email addresses with different display names.

    Groups addresses by their normalized email (ignoring display names),
    sums their message counts, and tracks the number of variations.

    Can return data in two formats for backward compatibility and feature expansion:
    - Default (preserve_variations=False): Returns old format with variation count
    - New (preserve_variations=True): Returns EmailGroup objects with full variation details

    Args:
        addresses: List of (email_address_with_display_name, count) tuples
        preserve_variations: If True, return detailed EmailGroup objects with variations.
                            If False (default), return legacy format for backward compatibility.

    Returns:
        If preserve_variations=False:
            List of (normalized_email, total_count, variation_count) tuples,
            sorted by total_count descending

        If preserve_variations=True:
            List of EmailGroup objects with full DisplayNameVariation details,
            sorted by total_count descending

    Example:
        >>> addresses = [
        ...     ('ClearScore <marketing@clearscore.com>', 582),
        ...     ('"ClearScore" <marketing@clearscore.com>', 404),
        ...     ('Clearscore <marketing@clearscore.com>', 17),
        ...     ('updates@clearscore.com', 1008),
        ... ]

        # Default backward-compatible format
        >>> consolidate_email_addresses(addresses)
        [
            ('marketing@clearscore.com', 1003, 3),
            ('updates@clearscore.com', 1008, 1)
        ]

        # New detailed format with variations
        >>> consolidate_email_addresses(addresses, preserve_variations=True)
        [
            EmailGroup(email='marketing@clearscore.com', total_count=1003, variations=[...]),
            EmailGroup(email='updates@clearscore.com', total_count=1008, variations=[...])
        ]
    """
    from collections import defaultdict

    # Group by normalized email
    email_data = defaultdict(lambda: {'count': 0, 'variations': {}})

    for addr, count in addresses:
        normalized = extract_email_address(addr)
        email_data[normalized]['count'] += count
        # Store with full address as key to preserve original variation
        if addr not in email_data[normalized]['variations']:
            email_data[normalized]['variations'][addr] = 0
        email_data[normalized]['variations'][addr] += count

    if preserve_variations:
        # Return new format with detailed variation information
        result = []
        for email, data in email_data.items():
            # Create DisplayNameVariation objects for each variation
            variations = [
                DisplayNameVariation(
                    full_address=full_addr,
                    display_name=_extract_display_name(full_addr),
                    count=count
                )
                for full_addr, count in data['variations'].items()
            ]
            # Sort variations by count descending
            variations.sort(key=lambda v: v.count, reverse=True)

            # Create EmailGroup with consolidated data
            email_group = EmailGroup(
                email=email,
                total_count=data['count'],
                variations=variations
            )
            result.append(email_group)

        # Sort by total_count descending
        return sorted(result, key=lambda x: x.total_count, reverse=True)
    else:
        # Return legacy format for backward compatibility
        result = [
            (email, data['count'], len(data['variations']))
            for email, data in email_data.items()
        ]

        # Sort by count descending
        return sorted(result, key=lambda x: x[1], reverse=True)
