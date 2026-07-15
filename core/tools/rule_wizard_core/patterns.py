"""Pattern extractors that mine the cache for email/subject rule candidates."""
from __future__ import annotations

from typing import List, Tuple

from core.tools.rule_wizard_core.addresses import _extract_display_name, extract_email_address

from core.tools.rule_wizard_core.cache_query import CacheQueryEngine


class EmailPatternExtractor:
    """Extract and suggest email address patterns for rule creation.

    Given an email address, this class suggests progressively broader patterns
    (exact match, wildcard TLD, domain only, domain base) along with estimated
    match counts from the cache.

    The pattern suggestions help users create rules that match the right scope
    of messages - from very specific (one sender) to very broad (entire domain).
    """

    def suggest_patterns(
        self, email_addr: str, cache_engine: CacheQueryEngine, fast_mode: bool = False
    ) -> List[Tuple[str, str, int]]:
        """Suggest email patterns based on the given email address or display name variation.

        Handles both plain email addresses and full addresses with display names
        (e.g., "Name <email@domain.com>"). When display names are present, suggests
        patterns that include the display name for differentiation.

        Args:
            email_addr: Email address to extract patterns from. Can be:
                       - Plain: "noreply@amazon.com"
                       - With display name: "Amazon Support <noreply@amazon.com>"
            cache_engine: Cache query engine for getting match counts
            fast_mode: If True, skip pattern effectiveness checking and return first pattern immediately

        Returns:
            List of tuples: (pattern, description, estimated_count)
            Only includes patterns that differ from the original email and provide
            broader matching capabilities.

        Example:
            >>> extractor = EmailPatternExtractor()
            >>> patterns = extractor.suggest_patterns("noreply@amazon.com", cache)
            >>> for pattern, desc, count in patterns:
            ...     print(f"{pattern:30} {desc:20} {count:5} messages")
            noreply@amazon.com             Exact match          45 messages
            noreply@amazon.*               All TLDs            127 messages
            @amazon.com                    All from domain     203 messages
            amazon                         All amazon domains  298 messages

            >>> # With display name
            >>> patterns = extractor.suggest_patterns("Amazon Support <noreply@amazon.com>", cache)
            Amazon Support <noreply@amazon.com>  With display name  12 messages
            Amazon Support                       Display name only   14 messages
            noreply@amazon.com                   Email only         45 messages
        """
        if not email_addr:
            return []

        email_lower = email_addr.lower().strip()
        patterns: List[Tuple[str, str, int]] = []

        # Extract display name if present
        display_name = _extract_display_name(email_addr)
        clean_email = extract_email_address(email_addr)

        # In fast mode, skip effectiveness checking and return just the email as-is
        if fast_mode:
            return [(email_lower, "Address as-is", 0)]

        # If this has a display name variation, add patterns for it
        if display_name and display_name.lower() != clean_email.lower():
            # 1. Full address with display name (highest precision)
            full_count = cache_engine.count_from_contains(email_lower)
            patterns.append((email_lower, "Full address (with display name)", full_count))

            # 2. Display name only (if different from email)
            display_name_lower = display_name.lower()
            display_count = cache_engine.count_from_contains(display_name_lower)
            if display_count > 0:
                patterns.append((display_name_lower, "Display name only", display_count))

        # Now add patterns for just the email address part
        email_lower = clean_email.lower()

        # Handle case where there's no @ sign
        if '@' not in email_lower:
            # If no @ sign, treat whole thing as domain pattern
            domain_base = email_lower.split('.')[0] if '.' in email_lower else email_lower
            if domain_base:
                count = cache_engine.count_from_pattern(f"*{domain_base}*")
                patterns.append((domain_base, f"All {domain_base} domains", count))
            return patterns

        # Extract local and domain parts
        local_part, domain_part = email_lower.rsplit('@', 1)

        # 1. Exact match
        exact_count = cache_engine.count_from_contains(email_lower)
        patterns.append((email_lower, "Exact match", exact_count))

        # 2. Wildcard TLD (if domain has a TLD)
        if '.' in domain_part:
            domain_without_tld = domain_part.rsplit('.', 1)[0]
            wildcard_tld = f"{local_part}@{domain_without_tld}.*"
            wildcard_count = cache_engine.count_from_pattern(wildcard_tld)
            # Only include if different from exact match
            if wildcard_count != exact_count or wildcard_count > exact_count:
                patterns.append((wildcard_tld, "All TLDs", wildcard_count))

        # 3. Domain only (any sender from this domain)
        domain_only = f"@{domain_part}"
        domain_count = cache_engine.count_from_contains(domain_only)
        # Only include if broader than previous patterns
        if domain_count > exact_count:
            patterns.append((domain_only, "All from domain", domain_count))

        # 4. Domain base (all related domains)
        domain_base = domain_part.split('.')[0]
        if domain_base and domain_base != domain_part:
            base_count = cache_engine.count_from_contains(domain_base)
            # Only include if broader than domain-only pattern
            if base_count > domain_count:
                patterns.append((domain_base, f"All {domain_base} domains", base_count))

        return patterns


class SubjectPatternExtractor:
    """Extract and suggest subject line patterns for rule creation.

    Given a subject line, this class suggests progressively broader patterns
    (exact match, without numbers, first N words, keywords) along with estimated
    match counts from the cache.

    The pattern suggestions help users create rules that match similar messages
    while filtering out variable content like order numbers or tracking IDs.
    """

    def suggest_patterns(
        self, subject: str, cache_engine: CacheQueryEngine, fast_mode: bool = False
    ) -> List[Tuple[str, str, int]]:
        """Suggest subject patterns based on the given subject line.

        Args:
            subject: Subject line to extract patterns from
            cache_engine: Cache query engine for getting match counts
            fast_mode: If True, skip pattern effectiveness checking and return first pattern immediately

        Returns:
            List of tuples: (pattern, description, estimated_count)
            Only includes patterns that differ meaningfully from the original
            and would match a broader set of messages.

        Example:
            >>> extractor = SubjectPatternExtractor()
            >>> patterns = extractor.suggest_patterns(
            ...     "Your Booking Confirmation For BRS-SRS-36558426",
            ...     cache
            ... )
            >>> for pattern, desc, count in patterns:
            ...     print(f"{pattern:50} {desc:25} {count:4} messages")
            Your Booking Confirmation For BRS-SRS-36558426     Exact match               1 messages
            Your Booking Confirmation For BRS-SRS-*            Without numbers          15 messages
            Your Booking Confirmation                          First 3 words            23 messages
            Booking                                            Keyword: Booking         45 messages
        """
        if not subject:
            return []

        import re

        subject_clean = subject.strip()

        if not subject_clean:
            return []

        # In fast mode, skip effectiveness checking and return just the subject as-is
        if fast_mode:
            return [(subject_clean, "Subject as-is", 0)]

        patterns: List[Tuple[str, str, int]] = []

        # 1. Exact match
        exact_count = cache_engine.count_subject_contains(subject_clean)
        patterns.append((subject_clean, "Exact match", exact_count))

        # 2. Without numbers (replace number sequences with wildcards)
        # Look for sequences of digits, possibly with separators
        without_numbers = re.sub(r'\b\d+\b', '*', subject_clean)
        # Handle codes like BRS-SRS-36558426 or ABC-123-DEF
        without_numbers = re.sub(r'[A-Z]+-[A-Z]+-\d+', '*', without_numbers)
        without_numbers = re.sub(r'[A-Z]+\d+', '*', without_numbers)  # Handle ABC123
        without_numbers = re.sub(r'\*+', '*', without_numbers)  # Collapse multiple wildcards
        without_numbers = without_numbers.strip()

        # Only include if different and not just a wildcard
        if without_numbers != subject_clean and without_numbers and without_numbers != '*':
            no_num_count = cache_engine.count_subject_contains(without_numbers.replace('*', ''))
            if no_num_count > exact_count:
                patterns.append((without_numbers, "Without numbers", no_num_count))

        # 3. First N words (try 3, then 2)
        words = subject_clean.split()

        if len(words) >= 3:
            # Try first 3 words
            first_3 = ' '.join(words[:3])
            if first_3 != subject_clean:
                first_3_count = cache_engine.count_subject_contains(first_3)
                if first_3_count > exact_count:
                    patterns.append((first_3, "First 3 words", first_3_count))

        if len(words) >= 2:
            # Try first 2 words (only if we haven't added first 3, or if meaningfully different)
            first_2 = ' '.join(words[:2])
            already_added = any(p[0] == first_2 for p in patterns)
            if not already_added and first_2 != subject_clean:
                first_2_count = cache_engine.count_subject_contains(first_2)
                # Only add if it provides more matches than exact
                if first_2_count > exact_count:
                    patterns.append((first_2, "First 2 words", first_2_count))

        # 4. Extract keywords (capitalized words > 3 chars, or longest word)
        keywords = [
            word for word in words
            if len(word) > 3 and (word[0].isupper() or word.isupper())
        ]

        # Filter out common words
        common_words = {
            'your', 'the', 'this', 'that', 'from', 'with', 'for', 'and',
            'are', 'was', 'were', 'been', 'have', 'has', 'had', 'will',
            'would', 'should', 'could', 'may', 'might', 'must', 'can',
            'when', 'where', 'what', 'which', 'who', 'whom', 'whose'
        }
        keywords = [kw for kw in keywords if kw.lower() not in common_words]

        # If no keywords found, try finding the longest meaningful word
        if not keywords and words:
            longest = max(words, key=len)
            if len(longest) > 3 and longest.lower() not in common_words:
                keywords = [longest]

        # Only suggest up to 2 most relevant keywords
        for keyword in keywords[:2]:
            # Skip if keyword is same as exact match
            if keyword == subject_clean:
                continue

            # Skip if we've already added this as a pattern
            already_added = any(p[0] == keyword for p in patterns)
            if already_added:
                continue

            kw_count = cache_engine.count_subject_contains(keyword)
            # Only add if it provides more matches than exact
            if kw_count > exact_count:
                patterns.append((keyword, f"Keyword: {keyword}", kw_count))

        return patterns
