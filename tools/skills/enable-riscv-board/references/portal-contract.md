# Portal ⇄ board pipeline contract

How the results portal (`farazahmad204/RISC-V-CI-Django-Web-Portal`, separate
repo) drives `scripts/board_pipeline.py` through the `riscv-board-pipeline`
Jenkins job, and what it gets back. This repo defines the contract; the portal
form, its upload storage and the results endpoint are built in the portal repo.

```
portal form ──(uploads stored by the portal, sha256 recorded)──┐
   │ POST /job/riscv-board-pipeline/buildWithParameters          │
   ▼                                                              ▼
Jenkins job ── board_pipeline.py ── downloads {url, sha256} docs from the portal host
   │  REPORT.md, result.json, generated/, build/out/ (archived artifacts)
   └─ PUBLISH_RESULT=true: POST result.json ──► portal results endpoint
```

## 1. Form → board request

The portal turns its form into the YAML passed as `BOARD_REQUEST_YAML`
(example: `assets/board_request.example.yaml`). The job overrides
`runner_checkout`, `act_checkout` and `request_id`, and drops `base_spec`,
`manifest` and `research_dirs`, so a portal request always starts from
evidence only.

| Form field | Request key | Notes |
|---|---|---|
| Board id | `board_id` | `[a-z0-9_]`, 2–63 chars; becomes the runner preset name |
| Board name | `board_name` | free text |
| ISA string | `isa` | e.g. `rv64imafdc_zicsr_zifencei_zba_zbb`; treated as a user answer |
| Documentation | `docs_quality` | `complete`, `vague` or `none` (reporting only) |
| Documents (TRM, schematic, datasheets) | `documents: [{url, sha256}]` | URLs must be on an allowed host (`BOARD_PIPELINE_URL_HOSTS`); the sha256 is checked after download |
| Device tree (DTB) | `dtb: {url, sha256}` → see note | |
| Stock boot logs | `boot_logs: {opensbi, linux, uboot}` | UART captures of a stock boot |
| Known facts | `user_answers: {<requirement id>: value}` | IDs from `assets/runner-requirements.yaml` |
| Serial device, boot packaging | `spec_overrides: {runner.serial_dev, boot.pattern, boot.image, ...}` | bench facts only the user knows |
| Runner hart (if known) | `runner_hart` | |
| Allow model use | job parameter `ALLOW_MODEL_CALLS` | sends document excerpts to Claude |

Note: `dtb` and `boot_logs` are paths in this version; until URL support is
added for them, the portal should place them in a shared directory on the
agent. `documents` already accept `{url, sha256}`.

## 2. Starting a run

`POST https://192.168.100.150/job/riscv-board-pipeline/buildWithParameters`
with a Jenkins API token, and these parameters:

| Parameter | Value |
|---|---|
| `REQUEST_ID` | portal request id (reuse it to resume) |
| `BOARD_REQUEST_YAML` | the request |
| `ALLOW_MODEL_CALLS` | from the form (default false) |
| `PUBLISH_RESULT` | true once the portal endpoint exists |

The build is SUCCESS when the board reached `offline_prepared`, UNSTABLE when
it stopped for a person (the normal outcome for a new board), FAILURE on an
error.

## 3. Result (`result.json`, schema_version 1)

| Key | Meaning |
|---|---|
| `board_id`, `board_name`, `request_id`, `isa`, `docs_quality` | echo of the request |
| `completion_state` | `researching` or `offline_prepared` |
| `stopped_at`, `stop_reason` | the step that needs a person, if any |
| `steps.<step>.status` | `ok`, `warn`, `skipped`, `incomplete`, `blocked`, `not_run` for extract, resolve, fill_gaps, resolve_research, decide, spec, runner_files, act_config, build, validate, report |
| `needs_person[]` | plain-language items: blockers, decisions and document values to confirm, missing ACT inputs |
| `runner` | `built`, `build_id`, `firmware_bin_sha256`, `text_data_bss`, `packaged` |
| `act` | `status`, `hypervisor` policy, `suites` (priv/unpriv/vector/hypervisor counts), `unverified_defaults`, `missing`, `validation` |
| `model` | `live`, `asked`, `answered`, `tokens` (subscription or API usage) |
| `artifacts[]` | archived files to link from the portal page |

Suggested portal page: state badge, the `needs_person` list as a checklist,
step table, runner/ACT summary, links to the archived `REPORT.md`,
`spec_review.md`, `generated/` and `build/out/`.

## 4. Results endpoint (to build in the portal)

`POST /portal/api/v1/board-requests/results/`, header
`Authorization: Token <riscv-portal-ingest-token>`, body `result.json`.
Key results by `request_id` and keep each build as a version. The job only
posts when `PUBLISH_RESULT=true`; enabling it is a production change and needs
approval.

## 5. What a portal run never does

No flashing, power control, pushes, pull requests or changes to other Jenkins
jobs. Hardware smoke, install and ACT runs on the board stay separate,
approved steps (`SKILL.md` phases 5–9).
