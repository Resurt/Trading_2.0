# Documentation Index

Status: current, updated 2026-06-30.

This directory contains current source-of-truth docs, runbooks, ADRs, research notes
and archived historical material.

Current source-of-truth docs:

- `Docs/architecture.md`
- `Docs/api-contract.md`
- `Docs/database-schema.md`
- `Docs/frontend-dashboard-spec.md`
- `Docs/logging-analytics-spec.md`
- `Docs/broker-gateway.md`
- `Docs/runbooks/data-only-shadow.md`
- `Docs/runbooks/analytics-and-calibration-center.md`
- `Docs/runbooks/data-retention-policy.md`
- `Docs/runbooks/calibration.md`
- `Docs/runbooks/production-checklist.md`

Current research/audit notes:

- `Docs/research/2026-06-29-market-streaming-audit.md`

Runbooks describe operator workflows. ADRs describe accepted design decisions when present.

Historical docs and prompts are retained for traceability. Files under `Docs/prompts/` are
historical implementation prompts and are not current acceptance criteria unless another current
source-of-truth doc explicitly references them.

Archived closed or superseded documents live under `Docs/archive/`. They must not be deleted.
Archived files are historical context only and do not override current docs.

For current operator behavior:

- Dashboard market display works without Start through `/ws/market-feed` (`/ws/market`
  remains a compatibility alias).
- Start controls only persistent daily data-only logging.
- One accepted data-only Start is a daily intent that rolls morning -> main -> evening.
- Manual Stop cancels auto-resume.
- Known-invalid primary market-data rows are purged with manifest/audit metadata, not
  merely hidden by calibration flags.
