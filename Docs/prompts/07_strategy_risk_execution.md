# Step 07 Prompt: Strategy, Risk, Execution

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: реализовать каркас strategy engine, risk engine и execution engine.

Сделай:

- `StrategyEngine`;
- `RiskEngine`;
- `ExecutionEngine`;
- `ReconciliationService`;
- конфигурационно-управляемую стратегию-заглушку по 5m/10m/15m и session template;
- explicit blocker codes из `Docs/logging-analytics-spec.md`;
- причинную цепочку gate checks;
- order intent lifecycle;
- cancel/reject reason codes;
- тесты на blocker pipeline и reproducible execution decisions.

Не реализуй магическую прибыльную модель.
