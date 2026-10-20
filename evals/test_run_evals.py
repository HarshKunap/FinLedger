import json

from run_evals import evaluate_extraction_fixtures, evaluate_goldens


def test_structural_harness_fails_corrupted_fixture(tmp_path):
    fixture = tmp_path / "broken.json"
    fixture.write_text(json.dumps({"document_id": "x", "document_type": "not-valid", "vendor": {}}))
    report = evaluate_goldens(tmp_path, provider_mode="mock")
    assert report.total == 1
    assert report.passed == 0
    assert report.failures
    assert report.provider_mode == "mock"


def test_extraction_eval_runs_markdown_and_detects_output_mismatch(tmp_path):
    (tmp_path / "sample.md").write_text("# INVOICE\nVendor: Acme\nInv No: A-1\nTotal: 100", encoding="utf-8")
    expected = {
        "document_id": "doc-sample", "document_type": "purchase_invoice",
        "vendor": {"name": "Acme"}, "invoice_no": "WRONG", "total": 100,
    }
    (tmp_path / "sample.json").write_text(json.dumps(expected), encoding="utf-8")
    report = evaluate_extraction_fixtures(tmp_path, provider_mode="mock")
    assert report.total == 1
    assert report.matched == 0
    assert any("invoice_no" in failure for failure in report.failures)


def test_extraction_eval_matches_real_fixture_pair():
    fixture_dir = __import__("pathlib").Path(__file__).parent.parent / "packages" / "extract" / "tests" / "fixtures"
    report = evaluate_extraction_fixtures(fixture_dir, provider_mode="mock")
    assert report.total == 6
    assert report.matched == 6
