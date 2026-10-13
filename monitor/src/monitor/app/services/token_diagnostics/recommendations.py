# -*- coding: utf-8 -*-
"""Review advice only: records contain neither commands nor execution hooks."""

from typing import TypedDict

from .diagnostic_contract import RuleId


class Recommendation(TypedDict):
    action_code: str
    location: str
    prerequisites: tuple[str, ...]
    exceptions: tuple[str, ...]
    risks: tuple[str, ...]
    verification: tuple[str, ...]


RECOMMENDATIONS: dict[RuleId, Recommendation] = {
    "context_growth": {
        "action_code": "review_summary_and_budget",
        "location": "Context assembly and memory compaction",
        "prerequisites": (
            "Confirm growth attribution and compatible token units.",
        ),
        "exceptions": ("Long evidence may be required for the current task.",),
        "risks": ("Summarization can lose constraints or evidence.",),
        "verification": (
            "Compare task quality and retained evidence before changing budgets.",
        ),
    },
    "segment_carryover": {
        "action_code": "review_source_summary",
        "location": "Repeated source assembly",
        "prerequisites": ("Verify source identity, version and lifecycle.",),
        "exceptions": ("Repeated source may still support later reasoning.",),
        "risks": (
            "Observed repeated volume is not removable volume or predicted savings.",
        ),
        "verification": (
            "Check source citations and task correctness after a reviewed summary.",
        ),
    },
    "tool_context_volume": {
        "action_code": "review_tool_projection",
        "location": "Tool result projection into prepared context",
        "prerequisites": (
            "Verify actual injected result and real business Step completion.",
        ),
        "exceptions": (
            "Completed Step results may remain necessary downstream.",
        ),
        "risks": ("Projection can remove facts required by later Steps.",),
        "verification": (
            "Compare injected evidence, retained artifacts and downstream quality.",
        ),
    },
    "safe_tool_repeat": {
        "action_code": "review_readonly_reuse",
        "location": "Tool invocation boundary",
        "prerequisites": (
            "Verify readonly behavior, idempotency, cacheability and data version.",
            "Verify permissions, freshness and tenant isolation before considering reuse.",
        ),
        "exceptions": (
            "Polling, fresh reads and permission changes may require another call.",
        ),
        "risks": (
            "Readonly repetition alone does not justify skipping execution.",
        ),
        "verification": (
            "Compare results and authorization under realistic freshness changes.",
        ),
    },
    "suspected_loop": {
        "action_code": "inspect_retry_and_progress",
        "location": "Business Step orchestration and retry policy",
        "prerequisites": (
            "Verify deterministic Step identities and collect progress evidence.",
        ),
        "exceptions": (
            "Repeated actions may represent legitimate progress or polling.",
        ),
        "risks": (
            "Missing progress evidence cannot prove absence of progress.",
        ),
        "verification": (
            "Inspect Step transitions, error feedback and expected completion.",
        ),
    },
    "text_prefix_stability": {
        "action_code": "review_stable_structure",
        "location": "Client context assembly order and dynamic insertion points",
        "prerequisites": (
            "Confirm complete compatible history and reliable static source versions.",
        ),
        "exceptions": (
            "Dynamic ordering may be needed for instruction priority.",
        ),
        "risks": (
            "Text prefix bounds do not demonstrate Provider cache behavior or savings.",
        ),
        "verification": (
            "Measure client prefix bounds and verify instruction following after review.",
        ),
    },
    "static_context_volume": {
        "action_code": "review_ondemand_instructions_and_tools",
        "location": "Instruction and Tool Schema configuration",
        "prerequisites": (
            "Verify trusted static categories and complete attributed denominator.",
        ),
        "exceptions": (
            "Full instructions or schema may be necessary for capability selection.",
        ),
        "risks": (
            "Reducing instructions or schemas can harm correctness and tool selection.",
        ),
        "verification": (
            "Evaluate instruction compliance and tool selection on representative tasks.",
        ),
    },
    "cohort_usage_outlier": {
        "action_code": "establish_business_cohort_and_evaluation",
        "location": "Business task classification and quality evaluation",
        "prerequisites": (
            "Supply reliable type, complexity, quality and at least 30 eligible samples.",
        ),
        "exceptions": (
            "Agent or model rankings alone do not establish comparable tasks.",
        ),
        "risks": (
            "Incomparable cohorts produce misleading usage conclusions.",
        ),
        "verification": (
            "Review cohort eligibility and quality before implementing statistical analysis.",
        ),
    },
}
