"""Contract checks for invoice-free extraction failure labels."""

from extract.failures import (
    INVALID_MODEL_OUTPUT,
    MISSING_EXTRACT_INPUT,
    VISION_PAGE_CAP,
    ExtractFailure,
)


def test_failure_categories_are_fixed_strings():
    assert INVALID_MODEL_OUTPUT == "invalid_model_output"
    assert VISION_PAGE_CAP == "vision_page_cap"
    assert MISSING_EXTRACT_INPUT == "missing_extract_input"


def test_extract_failure_message_is_category_only():
    error = ExtractFailure(INVALID_MODEL_OUTPUT)
    assert str(error) == "invalid_model_output"
    assert error.category == "invalid_model_output"
