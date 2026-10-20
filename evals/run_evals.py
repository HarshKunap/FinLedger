import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from contracts.schemas import CanonicalInvoice
from extract.extract_worker import ExtractWorker
from usage.model_router import ModelRouter, Settings
from usage.usage_writer import UsageWriter


@dataclass(frozen=True)
class EvalReport:
    total: int
    passed: int
    failures: list[str]
    provider_mode: str

    @property
    def structural_validity_rate(self) -> float:
        return self.passed / self.total * 100 if self.total else 0.0


@dataclass(frozen=True)
class ExtractionEvalReport:
    total: int
    matched: int
    failures: list[str]
    provider_mode: str

    @property
    def exact_match_rate(self) -> float:
        return self.matched / self.total * 100 if self.total else 0.0


def evaluate_goldens(golden_dir: Path, provider_mode: str) -> EvalReport:
    failures = []
    files = sorted(golden_dir.glob("*.json"))
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            validated = CanonicalInvoice.model_validate(raw)
            round_trip = validated.model_dump(mode="json", exclude_unset=False)
            reparsed = CanonicalInvoice.model_validate(round_trip)
            if reparsed != validated:
                failures.append(f"{path.name}: schema round-trip changed the structure")
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            failures.append(f"{path.name}: {exc}")
    if not files:
        failures.append("No golden fixtures found")
    passed = sum(1 for path in files if not any(item.startswith(path.name + ":") for item in failures))
    return EvalReport(len(files), passed, failures, provider_mode)


def _differences(expected, actual, path=""):
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences = []
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}" if path else key
            if key not in expected or key not in actual:
                differences.append(child)
            else:
                differences.extend(_differences(expected[key], actual[key], child))
        return differences
    if isinstance(expected, list) and isinstance(actual, list):
        differences = []
        if len(expected) != len(actual):
            differences.append(f"{path}.length")
        for index, (left, right) in enumerate(zip(expected, actual)):
            differences.extend(_differences(left, right, f"{path}[{index}]"))
        return differences
    return [] if expected == actual else [path]


def evaluate_extraction_fixtures(fixtures_dir: Path, provider_mode: str) -> ExtractionEvalReport:
    failures = []
    markdown_files = sorted(fixtures_dir.glob("*.md"))
    if provider_mode != "mock":
        return ExtractionEvalReport(len(markdown_files), 0, ["Extraction evaluation currently requires provider mode 'mock'"], provider_mode)
    for markdown_path in markdown_files:
        expected_path = markdown_path.with_suffix(".json")
        if not expected_path.exists():
            failures.append(f"{markdown_path.name}: missing expected JSON")
            continue
        try:
            expected = CanonicalInvoice.model_validate_json(expected_path.read_text(encoding="utf-8"))
            worker = ExtractWorker(UsageWriter(), ModelRouter(Settings(provider="mock")))
            generated = worker.extract("eval-client", expected.document_id, markdown_path.read_text(encoding="utf-8"))
            expected_data = expected.model_dump(mode="json")
            generated_data = generated.model_dump(mode="json")
            changed = _differences(expected_data, generated_data)
            if changed:
                failures.append(f"{markdown_path.name}: output mismatch at {', '.join(changed[:12])}")
        except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{markdown_path.name}: {exc}")
    matched = sum(1 for path in markdown_files if not any(item.startswith(path.name + ":") for item in failures))
    if not markdown_files:
        failures.append("No markdown extraction fixtures found")
    return ExtractionEvalReport(len(markdown_files), matched, failures, provider_mode)


def render_report(report: EvalReport, extraction: ExtractionEvalReport) -> str:
    status = "PASS" if report.total and not report.failures and extraction.total and not extraction.failures else "FAIL"
    lines = ["# FinLedger Person 1 Deterministic Evaluation Report", "",
        "> Structural validity and deterministic mock extraction are separate metrics. This does not claim live-model accuracy.", "",
        f"- **Status:** {status}", f"- **Provider mode:** `{report.provider_mode}`",
        f"- **Generated (UTC):** {datetime.now(timezone.utc).isoformat()}",
        "", "## Structural schema validation", "",
        f"- **Golden fixtures:** {report.total}", f"- **Structurally valid:** {report.passed}",
        f"- **Structural validity rate:** {report.structural_validity_rate:.2f}%", "",
        "## Deterministic mock extraction", "",
        f"- **Source/expected pairs:** {extraction.total}", f"- **Normalized exact matches:** {extraction.matched}",
        f"- **Normalized exact-match rate:** {extraction.exact_match_rate:.2f}%", "", "## Failures", ""]
    lines.extend(f"- Structural: {failure}" for failure in report.failures)
    lines.extend(f"- Extraction: {failure}" for failure in extraction.failures)
    if not report.failures and not extraction.failures:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-dir", type=Path, default=Path(__file__).parent.parent / "packages" / "contracts" / "golden")
    parser.add_argument("--fixtures-dir", type=Path, default=Path(__file__).parent.parent / "packages" / "extract" / "tests" / "fixtures")
    parser.add_argument("--report", type=Path, default=Path(__file__).parent / "latest_report.md")
    parser.add_argument("--provider-mode", default=os.getenv("FINLEDGER_LLM_PROVIDER", "mock"))
    parser.add_argument(
        "--live-synthetic-eval",
        action="store_true",
        help="Opt-in synthetic live-provider benchmark (off by default; not for CI).",
    )
    args = parser.parse_args(argv)
    if args.provider_mode != "mock" and not args.live_synthetic_eval:
        print(
            "FAIL: non-mock provider mode requires --live-synthetic-eval (owner-approved account only)",
            file=sys.stderr,
        )
        return 1
    if args.live_synthetic_eval and args.provider_mode == "mock":
        print("FAIL: --live-synthetic-eval requires a non-mock --provider-mode", file=sys.stderr)
        return 1
    report = evaluate_goldens(args.golden_dir, args.provider_mode)
    extraction = evaluate_extraction_fixtures(args.fixtures_dir, args.provider_mode)
    args.report.write_text(render_report(report, extraction), encoding="utf-8")
    print(f"Structural goldens: {report.passed}/{report.total} valid; provider={report.provider_mode}")
    print(f"Deterministic extraction: {extraction.matched}/{extraction.total} normalized exact matches")
    for failure in report.failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    for failure in extraction.failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 0 if report.total and not report.failures and extraction.total and not extraction.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
