# Step 12 Prompt: Controlled Launch

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: довести проект до controlled launch.

Сделай:

- historical replay;
- sandbox trading;
- shadow mode on live market data;
- production mode guardrails;
- replay harness для candles/events, session rollovers, blockers, counterfactual pipelines;
- sandbox smoke tests;
- shadow mode без реального `PostOrder`;
- CI pipeline: lint, type check, backend tests, frontend tests, migration checks, smoke tests;
- runbooks для token rotation и report rebuilds, если они ещё отсутствуют.
