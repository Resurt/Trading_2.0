# Analytics And Calibration Center

This runbook defines two diagnostic zones for the robot: Intraday Analytics and Calibration
Center. Both zones are observer/reporting surfaces. They do not make live trading decisions,
do not place orders, and do not apply runtime configuration.

## Intraday Analytics

Intraday Analytics explains the current trading day by session, hour, instrument, timeframe and
side. It is designed for operator situational awareness during data-only shadow, historical
replay, strategy shadow, sandbox and live observation modes.

Canonical session scopes:

- `weekday_morning`
- `weekday_main`
- `weekday_evening`
- `weekend`

The page and API show:

- current `trading_date` and `calendar_date`;
- session status: `not_started`, `running`, `completed`;
- micro-session/hour buckets inside each session;
- instrument x timeframe x side rows for `long`, `short` and `all`;
- market trend/bias: `long_bias`, `short_bias`, `sideways`, `mixed`, `unknown`;
- market activity: `low`, `normal`, `high`, `unknown`;
- spread, depth, imbalance and market-quality summaries;
- stale-data incidents and candle lag;
- candidate, pseudo-order, real-order, blocker and near-miss counts;
- why there were no trades or no signals;
- what was closest to entry, based on final blockers and counterfactual near misses;
- warnings for facts that cannot be interpreted because the sample is small.

Intraday Analytics may use live microstructure collected by data-only shadow, but it does not
turn that data into live entry decisions. A low-activity morning or a quiet main session is an
explanation, not a permission to loosen risk gates automatically.

## Calibration Center

Calibration Center is the longer-horizon diagnostic surface for robot health and contour
quality. It joins rolling statistics, no-trade diagnostics, regime/drift checks and candidate
configuration proposals.

The page and API show:

- rolling performance cubes over `7d`, `20d`, `60d`, `90d`, `180d`, `365d`;
- robot health by instrument/session/timeframe/side/mode;
- no-trade diagnostics;
- market regime snapshots;
- spread/depth/volatility drift;
- blocker drift;
- data-quality and stale-feed warnings;
- top contours and dead contours;
- candidate config proposals;
- approve/reject workflow for proposals.

Supported diagnostic outcomes:

- `market_dead`: no signals/trades while market activity is low across the selected universe.
- `robot_too_strict`: market activity is normal, but blockers increased materially.
- `data_quality_problem`: missing, stale or gapped data dominates the period.
- `regime_changed`: spread, depth or volatility changed materially.
- `not_enough_data`: sample size is too small for a calibration conclusion.
- `normal_no_action_needed`: market and robot health look normal.
- `calibration_recommended`: calibration should be reviewed, but not applied automatically.

## Safety Policy

- Intraday Analytics is diagnostic only. It does not enable trading.
- Calibration Center does not apply live config automatically.
- Candidate configs are stored as `draft` proposals first.
- Approval changes proposal status only; applying a proposal to actual runtime is a separate
  future workflow.
- Operator/admin approval is required before any config can be used outside proposal storage.
- A small sample must not permanently disable a timeframe, session, side or instrument.
- 10-20 trading days of data-only shadow are early evidence, not final truth.
- Data-only shadow evidence can recommend more data collection or strategy shadow review, but it
  is not a claim that a strategy is ready.
- Real `PostOrder` and real `CancelOrder` are outside this workflow.
