"""Stable, invoice-free extraction failure categories for queue mapping."""

INVALID_MODEL_OUTPUT = "invalid_model_output"
VISION_PAGE_CAP = "vision_page_cap"
MISSING_EXTRACT_INPUT = "missing_extract_input"
STATEMENT_NOT_EXTRACTED = "statement_not_extracted"
PROVIDER_TRANSIENT = "provider_transient"
AI_BUDGET_EXCEEDED = "ai_budget_exceeded"
OBJECT_KEY_REJECTED = "object_key_rejected"

FAILURE_CATEGORIES = frozenset({
    INVALID_MODEL_OUTPUT,
    VISION_PAGE_CAP,
    MISSING_EXTRACT_INPUT,
    STATEMENT_NOT_EXTRACTED,
    PROVIDER_TRANSIENT,
    AI_BUDGET_EXCEEDED,
    OBJECT_KEY_REJECTED,
})


class ExtractFailure(Exception):
    """Raised when extraction cannot proceed; message is the category label only."""

    def __init__(self, category: str):
        if category not in FAILURE_CATEGORIES:
            raise ValueError(f"unknown extraction failure category: {category}")
        self.category = category
        super().__init__(category)
