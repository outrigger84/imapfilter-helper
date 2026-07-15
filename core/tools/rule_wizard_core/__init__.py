"""Core components for the IMAPFilter rule creation wizard.

Package split of the former single-module rule_wizard_core.py. All
public names remain importable from core.tools.rule_wizard_core.
"""
from core.ui_components import format_count, prompt_yes_no
from core.tools.rule_wizard_core.addresses import (
    DisplayNameVariation,
    EmailGroup,
    _extract_display_name,
    compute_domain_counts,
    consolidate_email_addresses,
    create_expandable_email_items,
    extract_email_address,
    get_emails_for_domain,
)
from core.tools.rule_wizard_core.builder import (
    ConditionGroup,
    ConditionNode,
    GroupingSpec,
    RuleBuilder,
    save_rule,
)
from core.tools.rule_wizard_core.cache_query import CacheQueryEngine, safe_parse_header
from core.tools.rule_wizard_core.coverage_io import (
    _deserialize_coverage_data,
    _serialize_coverage_data,
)
from core.tools.rule_wizard_core.patterns import (
    EmailPatternExtractor,
    SubjectPatternExtractor,
)
from core.tools.rule_wizard_core.selector import ExpandableItem, FilterableListSelector
from core.tools.rule_wizard_core.wizard import RuleWizard

__all__ = [
    "CacheQueryEngine",
    "ConditionGroup",
    "ConditionNode",
    "DisplayNameVariation",
    "EmailGroup",
    "EmailPatternExtractor",
    "ExpandableItem",
    "FilterableListSelector",
    "GroupingSpec",
    "RuleBuilder",
    "RuleWizard",
    "SubjectPatternExtractor",
    "compute_domain_counts",
    "consolidate_email_addresses",
    "create_expandable_email_items",
    "extract_email_address",
    "format_count",
    "get_emails_for_domain",
    "prompt_yes_no",
    "safe_parse_header",
    "save_rule",
]
