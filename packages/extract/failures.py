"""Stable, invoice-free extraction failure categories for queue mapping."""

INVALID_MODEL_OUTPUT = "invalid_model_output"
VISION_PAGE_CAP = "vision_page_cap"
MISSING_EXTRACT_INPUT = "missing_extract_input"


class ExtractFailure(Exception):
    """Raised when extraction cannot proceed; message is the category label only."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)
