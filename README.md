# FinLedger AP — 4 person packs

Architecture baseline: 23 Sep 2026. Hard rule: extraction never posts.

| Pack | Seat | Markdown | PDF |
|---|---|---|---|
| Person 1 | AI / RAG / tokens | `person1_ai_rag/ARCHITECTURE.md` | `person1_ai_rag/Person1_AI_RAG.pdf` |
| Person 2 | Platform / intake / data | `person2_platform/ARCHITECTURE.md` | `person2_platform/Person2_Platform.pdf` |
| Person 3 | Control + review UI | `person3_control_ui/ARCHITECTURE.md` | `person3_control_ui/Person3_Control_UI.pdf` |
| Person 4 | Tally connector | `person4_tally/ARCHITECTURE.md` | `person4_tally/Person4_Tally_Connector.pdf` |

Shared objects: `shared/00_SHARED_CONTRACTS.md` and `shared/Shared_Contracts.pdf`.

The workspace contains the work for Persons 1–4. This handoff covers Persons 1–3 only;
Person 4 has separately contributed connector code and is not assessed here.

## Helping finish FinLedger

Start with [the three-person work split](docs/TEAM_WORK_SPLIT.md): AI, platform/deployment,
and controls/QA. Read [HANDOVER.md](HANDOVER.md) before changes. Do not rebuild completed work,
share `.env`, or edit `person4_tally/` as part of these assignments.

## Capability status

| Seat | Status | Source | Validation |
|---|---|---|
| Person 1 | Prototype | `packages/`, `evals/` | 43 tests + deterministic fixture evaluation |
| Person 2 | Implemented | `person2_platform/` | 333 tests |
| Person 3 | Implemented | `person3_control_ui/` | 55 tests |
| Person 4 | Separately maintained; not assessed here | `person4_tally/` | See that pack's own evidence |

`Implemented` means runnable and integration-tested. `Prototype` means runnable
with deterministic/in-memory development adapters. `Planned` means no runtime
implementation exists. The complete vendor-to-Tally loop is therefore not yet
production-executable: Person 1 still needs live provider/persistent adapters,
and connector release readiness is outside this P1–P3 assessment.

## Prerequisites and verification

- Python 3.12 (the locked native dependencies do not support Python 3.14).
- `uv` for the Person 1–3 dependency environments.
- PostgreSQL tools (`initdb`, `pg_ctl`, `psql`) on `PATH` for integration tests.
- EICAR scanner mode for local tests; `clamd` is required in production.

Staging/production reject EICAR-only scanner configuration at settings construction.
Unknown scanner names are rejected in all environments rather than silently selecting a backend.

From this repository root, run the complete verification suite with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/test.ps1
```

## Local mock environment

The repository root contains `.env.example` with placeholders for PostgreSQL,
Supabase, storage, email, Person 1 AI providers, and the Person 4 Tally agent.
Create a local copy only if `.env` does not already exist. Never overwrite existing credentials or distribute `.env`.

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
```

PowerShell does not automatically load `.env`. From this directory, load it into the current terminal before running either service:

```powershell
Get-Content .env | Where-Object { $_ -and -not $_.StartsWith('#') } | ForEach-Object {
  $name, $value = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($name, $value, 'Process')
}
```

Mock keys are suitable only for wiring and tests. Replace every `mock_`, `sk-mock`, `sk-ant-mock`, and local database password before deployment.

Release gates, staging proofs, scanner setup and safe rollback are documented in
[the deployment runbook](docs/DEPLOYMENT_RUNBOOK.md). This procedure does not mean a customer release is ready.

## Person 1 quick start

```powershell
uv sync --project person2_platform --python 3.12 --locked --all-extras --dev
$env:PYTHONPATH = "evals"
person2_platform/.venv/Scripts/python.exe -m pytest packages evals -q --basetemp=.pytest-person1
person2_platform/.venv/Scripts/python.exe evals/run_evals.py --provider-mode mock
```

Person 1 is installed as `finledger-ai`, including its prompts and schemas, in
both dependency environments and the runtime image. It currently uses deterministic
mock provider methods. The environment variables expose the future live-provider
boundary without requiring real keys. Packaging does not implement the extract queue
consumer or a live provider; staging and production continue to reject mock AI.

## Runtime order

```text
PostgreSQL → migrations/bootstrap → Person 2 API and workers
           → Person 1 mock pipeline → Person 3 review service
           → Person 4 connector (separately maintained; outside this handoff)
```
