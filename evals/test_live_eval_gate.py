from run_evals import main


def test_non_mock_provider_requires_live_flag():
    assert main(["--provider-mode", "gemini", "--report", "/tmp/eval-gate.md"]) == 1
