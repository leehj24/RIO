"""Foundational building blocks for the LLM agent workflow.

This package deliberately keeps model calls and order execution out of the
foundation layer.  Prompts describe roles, schemas validate model output, and
the stores retain an append-only record that can be audited later.
"""

from .audit_store import AppendOnlyAuditStore, AuditReadError
from .memory_store import AgentMemoryStore, MemoryReadError
from .prompt_loader import (
    PromptDefinition,
    PromptFrontMatterError,
    PromptLoader,
    PromptRenderError,
    load_prompt,
)
from .schemas import AgentDecision, DecisionValidationError, validate_agent_decision

__all__ = [
    "AgentDecision",
    "AgentMemoryStore",
    "AppendOnlyAuditStore",
    "AuditReadError",
    "DecisionValidationError",
    "MemoryReadError",
    "PromptDefinition",
    "PromptFrontMatterError",
    "PromptLoader",
    "PromptRenderError",
    "load_prompt",
    "validate_agent_decision",
]
