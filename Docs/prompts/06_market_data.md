# Step 06 Prompt: Market Data And Bar Engine

Перед началом прочитай `Docs/architecture.md`, `Docs/implementation-plan.md`, `Docs/logging-analytics-spec.md` и все ADR.

Задача шага: реализовать market data pipeline, bar engine и фундамент live dashboard.

Сделай:

- подписки на candles, order book, last prices, trading status/info, market trades, user order state stream;
- внутреннюю event bus модель внутри `trade-core`;
- bar engine для 5m/10m/15m closed bars;
- timestamps в UTC и exchange timezone;
- market state calculators: spread, mid price, best bid/ask, depth summary, book imbalance, market quality score, candle staleness, feed freshness;
- read models для API/frontend;
- replay tests для deterministic bars.
