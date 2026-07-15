"""Serialization of coverage-analysis results for the wizard cache file."""
from __future__ import annotations

from core.tools.coverage_analyzer import CoverageStats, DomainCluster


def _serialize_coverage_data(stats, uncovered_messages, domain_clusters):
    """Serialize coverage analysis results to JSON-compatible format.

    Converts CoverageStats, UncoveredMessage, and DomainCluster objects
    to dictionaries suitable for JSON storage.

    Args:
        stats: CoverageStats object
        uncovered_messages: List of UncoveredMessage objects
        domain_clusters: List of DomainCluster objects

    Returns:
        Dict with serialized coverage data
    """
    # Serialize uncovered messages
    serialized_uncovered = [
        {
            'uid': msg.uid,
            'folder': msg.folder,
            'from_address': msg.from_address,
            'subject': msg.subject,
            'domain': msg.domain,
        }
        for msg in uncovered_messages
    ]

    # Serialize domain clusters
    serialized_clusters = [
        {
            'domain': cluster.domain,
            'total_count': cluster.total_count,
            'senders': cluster.senders,
            'messages': [
                {
                    'uid': msg.uid,
                    'folder': msg.folder,
                    'from_address': msg.from_address,
                    'subject': msg.subject,
                    'domain': msg.domain,
                }
                for msg in cluster.messages
            ]
        }
        for cluster in domain_clusters
    ]

    return {
        'stats': {
            'total_messages': stats.total_messages,
            'covered_messages': stats.covered_messages,
            'uncovered_messages': stats.uncovered_messages,
            'coverage_by_rule': stats.coverage_by_rule,
        },
        'uncovered_messages': serialized_uncovered,
        'domain_clusters': serialized_clusters,
    }


def _deserialize_coverage_data(data):
    """Deserialize coverage data from JSON format back to objects.

    Converts stored dictionaries back to CoverageStats, UncoveredMessage,
    and DomainCluster objects.

    Args:
        data: Dict with serialized coverage data

    Returns:
        Tuple of (CoverageStats, List[UncoveredMessage], List[DomainCluster])
    """
    from core.tools.coverage_analyzer import CoverageStats, UncoveredMessage, DomainCluster

    # Deserialize stats
    stats_data = data['stats']
    stats = CoverageStats(
        total_messages=stats_data['total_messages'],
        covered_messages=stats_data['covered_messages'],
        uncovered_messages=stats_data['uncovered_messages'],
        coverage_by_rule=stats_data['coverage_by_rule'],
    )

    # Deserialize uncovered messages
    uncovered_messages = [
        UncoveredMessage(
            uid=msg['uid'],
            folder=msg['folder'],
            from_address=msg['from_address'],
            subject=msg['subject'],
            domain=msg['domain'],
        )
        for msg in data['uncovered_messages']
    ]

    # Deserialize domain clusters
    domain_clusters = [
        DomainCluster(
            domain=cluster['domain'],
            total_count=cluster['total_count'],
            senders=cluster['senders'],
            messages=[
                UncoveredMessage(
                    uid=msg['uid'],
                    folder=msg['folder'],
                    from_address=msg['from_address'],
                    subject=msg['subject'],
                    domain=msg['domain'],
                )
                for msg in cluster['messages']
            ]
        )
        for cluster in data['domain_clusters']
    ]

    return stats, uncovered_messages, domain_clusters
