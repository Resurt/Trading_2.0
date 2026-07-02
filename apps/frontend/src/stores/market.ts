import { computed, ref, watch } from "vue";
import { defineStore } from "pinia";

import { apiClient, openAuthenticatedWebSocket } from "../api/client";
import type {
  ConnectionState,
  DashboardMarketFeedSnapshot,
  DashboardMarketFeedStatus,
  DataShadowStatusResponse,
  JsonPayload,
  MarketInstrumentOverview,
  MarketOverviewResponse,
  WebSocketEnvelope,
} from "../api/types";

export const CORE_INSTRUMENT_IDS = [
  "MOEX:SBER",
  "MOEX:GAZP",
  "MOEX:LKOH",
  "MOEX:YDEX",
  "MOEX:TATN",
  "MOEX:GMKN",
  "MOEX:OZON",
  "MOEX:VTBR",
];

const DEFAULT_SELECTED_INSTRUMENT_ID = "MOEX:SBER";
const TRANSIENT_DASHBOARD_FEED_ERRORS = new Set([
  "request_timeout",
  "dashboard_market_feed_timeout",
]);
const LIVE_TRADE_TAPE_MAX_AGE_MS = 15 * 1000;
const DELAYED_TRADE_TAPE_DISPLAY_MAX_AGE_MS = 5 * 60 * 1000;
const SELECTED_ORDER_BOOK_MIN_SIDE_LEVELS = 5;
const SELECTED_ORDER_BOOK_RETRY_LIMIT = 30;
const SELECTED_ORDER_BOOK_RETRY_MS = 1000;
const CORE_INSTRUMENT_ORDER = new Map(
  CORE_INSTRUMENT_IDS.map((instrumentId, index) => [instrumentId, index]),
);

const EMPTY_OVERVIEW: MarketOverviewResponse = {
  generated_at: new Date(0).toISOString(),
  instruments: CORE_INSTRUMENT_IDS.map((instrumentId) => emptyInstrument(instrumentId)),
};

const EMPTY_DATA_SHADOW_STATUS: DataShadowStatusResponse = {
  enabled: false,
  collector_state: "stopped",
  day_collection_state: "inactive",
  daily_collection_active: false,
  current_window_state: "stopped",
  next_collection_window_at: null,
  remaining_windows_today: 0,
  collector_left_running: false,
  paused_at: null,
  completed_for_day_at: null,
  last_stop_reason: null,
  last_pause_reason: null,
  last_resume_at: null,
  last_window_completed_at: null,
  strategy_trading_disabled: true,
  real_orders_disabled: true,
  market_open: null,
  market_closed_expected: null,
  reason_code: null,
  next_session_at: null,
  stream_alive: false,
  last_message_age_seconds: null,
  candles_received: null,
  order_book_snapshots: 0,
  market_microstructure_snapshots: 0,
  avg_spread_bps: null,
  p95_spread_bps: null,
  avg_market_quality_score: null,
  current_session: null,
  collector_started_at: null,
  started_at: null,
  stopped_at: null,
  last_command_id: null,
  last_command_status: null,
  last_command_reason_code: null,
  instruments: [],
  stream_batches: [],
  supervisor_enabled: false,
  supervisor_state: "not_configured",
  stream_restart_count: 0,
  last_restart_at: null,
  last_restart_reason: null,
  stream_stale_count: 0,
  last_stream_error: null,
  per_stream_status: {},
  warnings: [],
  warning: null,
};

const EMPTY_DASHBOARD_FEED_STATUS: DashboardMarketFeedStatus = {
  enabled: true,
  running: false,
  market_open: false,
  session_type: "unknown",
  session_phase: "unknown",
  venue_type: "unknown",
  next_session_at: null,
  last_refresh_at: null,
  selected_instrument: DEFAULT_SELECTED_INSTRUMENT_ID,
  quote_rows_count: CORE_INSTRUMENT_IDS.length,
  order_book_available: false,
  trade_tape_available: false,
  errors: [],
  warnings: [],
};

export const useMarketStore = defineStore("market", () => {
  const overview = ref<MarketOverviewResponse>(EMPTY_OVERVIEW);
  const dataShadowStatus = ref<DataShadowStatusResponse>(EMPTY_DATA_SHADOW_STATUS);
  const dashboardFeedStatus = ref<DashboardMarketFeedStatus>(EMPTY_DASHBOARD_FEED_STATUS);
  const selectedInstrumentId = ref<string | null>(DEFAULT_SELECTED_INSTRUMENT_ID);
  const quoteBoardLoading = ref(false);
  const selectedDetailsLoading = ref(false);
  const feedErrors = ref<string[]>([]);
  const feedWarnings = ref<string[]>([]);
  const lastQuoteRefreshAt = ref<string | null>(null);
  const lastDetailsRefreshAt = ref<string | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const warnings = ref<string[]>([]);
  const liveConnection = ref<ConnectionState>("idle");
  let marketSocket: WebSocket | null = null;
  let marketPollTimer: number | null = null;
  let quoteRefreshTimer: number | null = null;
  let selectedDetailsTimer: number | null = null;
  let lastSelectedDetailsFetchAt = 0;
  let overviewInFlight = false;
  let dashboardFeedInFlight = false;
  let quoteRefreshInFlight = false;
  let selectedDetailsInFlight = false;
  let selectedTradesInFlight = false;
  let dataShadowStatusInFlight = false;
  let selectedDetailsRequestId = 0;
  let selectedOrderBookRetryCount = 0;
  let selectedOrderBookRetryTimer: number | null = null;

  const currentInstrument = computed<MarketInstrumentOverview | null>(() => {
    if (overview.value.instruments.length === 0) {
      return null;
    }
    const selected = overview.value.instruments.find(
      (instrument) => instrument.instrument_id === selectedInstrumentId.value,
    );
    if (selected) {
      return selected;
    }
    const defaultInstrument = overview.value.instruments.find(
      (instrument) => instrument.instrument_id === DEFAULT_SELECTED_INSTRUMENT_ID,
    );
    if (defaultInstrument) {
      return defaultInstrument;
    }
    return (
      overview.value.instruments.find((instrument) => CORE_INSTRUMENT_ORDER.has(instrument.instrument_id)) ??
      overview.value.instruments[0]
    );
  });

  const topOfBook = computed(() => ({
    bestBid: currentInstrument.value?.best_bid ?? null,
    bestAsk: currentInstrument.value?.best_ask ?? null,
    spread: currentInstrument.value?.spread ?? null,
    midPrice: currentInstrument.value?.mid_price ?? null,
  }));

  const bookSummaryRows = computed(() =>
    Object.entries(currentInstrument.value?.order_book_summary ?? {}).map(([key, value]) => ({
      key,
      value: String(value ?? "Нет данных"),
    })),
  );

  const recentTrades = computed<JsonPayload[]>(() => currentInstrument.value?.recent_market_trades ?? []);

  const quoteRows = computed(() => sortInstrumentRows(overview.value.instruments));

  async function fetchOverview(options: { silent?: boolean } = {}): Promise<void> {
    await fetchDashboardFeedSnapshot({
      silent: options.silent,
      includeOrderBook: false,
      includeTrades: false,
    });
  }

  async function refreshQuotes(): Promise<void> {
    if (quoteRefreshInFlight) {
      return;
    }
    quoteRefreshInFlight = true;
    try {
      await refreshDashboardFeed({
        includeOrderBook: false,
        includeTrades: false,
      });
      await refreshSelectedInstrumentDetails();
    } catch (unknownError) {
      if (overview.value.instruments.length === 0) {
        error.value = unknownError instanceof Error ? unknownError.message : "Market quote refresh failed";
      }
    } finally {
      quoteRefreshInFlight = false;
    }
  }

  async function fetchDashboardFeedSnapshot(options: {
    silent?: boolean;
    includeOrderBook?: boolean;
    includeTrades?: boolean;
  } = {}): Promise<void> {
    if (dashboardFeedInFlight || overviewInFlight) {
      return;
    }
    dashboardFeedInFlight = true;
    overviewInFlight = true;
    quoteBoardLoading.value = !options.silent;
    if (!options.silent) {
      loading.value = true;
    }
    error.value = null;
    try {
      const requestedInstrumentId = selectedInstrumentId.value ?? DEFAULT_SELECTED_INSTRUMENT_ID;
      const snapshot = await apiClient.dashboardMarketFeedSnapshot({
        selected_instrument: requestedInstrumentId,
        include_order_book: options.includeOrderBook ?? false,
        include_trades: options.includeTrades ?? false,
      });
      applyDashboardFeedSnapshot(snapshot, requestedInstrumentId);
    } catch (unknownError) {
      const reason = errorText(unknownError);
      if (isTransientDashboardFeedError(reason) && hasUsableDashboardFeedData()) {
        feedErrors.value = [];
        feedWarnings.value = boundedUnique([
          "dashboard_refresh_retrying",
          ...feedWarnings.value,
        ]);
      } else {
        recordWarning("dashboard_market_feed_unavailable");
        feedErrors.value = [reason];
      }
      if (overview.value.instruments.length === 0) {
        error.value = unknownError instanceof Error ? unknownError.message : "Dashboard market feed failed";
      }
    } finally {
      quoteBoardLoading.value = false;
      if (!options.silent) {
        loading.value = false;
      }
      dashboardFeedInFlight = false;
      overviewInFlight = false;
    }
  }

  async function refreshDashboardFeed(options: {
    includeOrderBook?: boolean;
    includeTrades?: boolean;
  } = {}): Promise<void> {
    const requestedInstrumentId = selectedInstrumentId.value ?? DEFAULT_SELECTED_INSTRUMENT_ID;
    const snapshot = await apiClient.refreshDashboardMarketFeed({
      selected_instrument: requestedInstrumentId,
      include_order_book: options.includeOrderBook ?? true,
      include_trades: options.includeTrades ?? true,
    });
    applyDashboardFeedSnapshot(snapshot, requestedInstrumentId);
  }

  async function refreshSelectedInstrumentDetails(): Promise<void> {
    if (selectedDetailsInFlight) {
      const pendingInstrumentId = selectedInstrumentId.value ?? currentInstrument.value?.instrument_id;
      if (pendingInstrumentId && shouldRetrySelectedOrderBook(currentInstrument.value)) {
        scheduleSelectedOrderBookRetry(pendingInstrumentId);
      }
      return;
    }
    const instrument = currentInstrument.value;
    const instrumentId = instrument?.instrument_id ?? selectedInstrumentId.value;
    if (!instrumentId) {
      return;
    }
    const requestId = ++selectedDetailsRequestId;
    selectedDetailsInFlight = true;
    selectedDetailsLoading.value = true;
    lastSelectedDetailsFetchAt = Date.now();
    try {
      const snapshot = await apiClient.dashboardMarketFeedSnapshot({
        selected_instrument: instrumentId,
        include_order_book: true,
        include_trades: false,
      });
      if (requestId === selectedDetailsRequestId && selectedInstrumentId.value === instrumentId) {
        clearWarning("selected_instrument_details_unavailable");
        applyDashboardFeedSnapshot(snapshot, instrumentId);
        const applied = currentInstrument.value;
        if (shouldRetrySelectedOrderBook(applied)) {
          scheduleSelectedOrderBookRetry(instrumentId);
        } else {
          selectedOrderBookRetryCount = 0;
          clearSelectedOrderBookRetry();
        }
        void refreshSelectedInstrumentTrades(instrumentId, requestId);
      } else if (snapshot.selected_details) {
        applyOverview({ generated_at: snapshot.generated_at, instruments: [snapshot.selected_details] });
      }
    } catch (unknownError) {
      if (requestId === selectedDetailsRequestId) {
        recordWarning("selected_instrument_details_unavailable");
        if (selectedInstrumentId.value === instrumentId && shouldRetrySelectedOrderBook(currentInstrument.value)) {
          scheduleSelectedOrderBookRetry(instrumentId);
        }
      }
      if (overview.value.instruments.length === 0) {
        error.value = unknownError instanceof Error ? unknownError.message : "Selected market details failed";
      }
    } finally {
      if (requestId === selectedDetailsRequestId) {
        selectedDetailsLoading.value = false;
        selectedDetailsInFlight = false;
      }
    }
  }

  async function refreshSelectedInstrumentTrades(
    instrumentId: string,
    requestId: number,
  ): Promise<void> {
    if (selectedTradesInFlight || selectedInstrumentId.value !== instrumentId) {
      return;
    }
    selectedTradesInFlight = true;
    try {
      const snapshot = await apiClient.dashboardMarketFeedSnapshot({
        selected_instrument: instrumentId,
        include_order_book: false,
        include_trades: true,
      });
      if (requestId === selectedDetailsRequestId && selectedInstrumentId.value === instrumentId) {
        applyDashboardFeedSnapshot(snapshot, instrumentId);
      }
    } catch {
      recordWarning("selected_market_trades_unavailable");
    } finally {
      selectedTradesInFlight = false;
    }
  }

  function scheduleSelectedOrderBookRetry(instrumentId: string): void {
    if (selectedOrderBookRetryCount >= SELECTED_ORDER_BOOK_RETRY_LIMIT) {
      return;
    }
    selectedOrderBookRetryCount += 1;
    clearSelectedOrderBookRetry();
    selectedOrderBookRetryTimer = window.setTimeout(() => {
      selectedOrderBookRetryTimer = null;
      if (selectedInstrumentId.value === instrumentId) {
        void refreshSelectedInstrumentDetails();
      }
    }, SELECTED_ORDER_BOOK_RETRY_MS);
  }

  function clearSelectedOrderBookRetry(): void {
    if (selectedOrderBookRetryTimer === null) {
      return;
    }
    window.clearTimeout(selectedOrderBookRetryTimer);
    selectedOrderBookRetryTimer = null;
  }

  function applyDashboardFeedSnapshot(
    snapshot: DashboardMarketFeedSnapshot,
    expectedSelectedInstrumentId?: string,
  ): void {
    const rawErrors = snapshot.errors ?? snapshot.status?.errors ?? [];
    const rawWarnings = snapshot.warnings ?? snapshot.status?.warnings ?? [];
    const hasUsableData = snapshotHasUsableDashboardFeedData(snapshot) || hasUsableDashboardFeedData();
    const transientErrors = rawErrors.filter(isTransientDashboardFeedError);
    const blockingErrors = rawErrors.filter(
      (item) => !(isTransientDashboardFeedError(item) && hasUsableData),
    );
    const visibleWarnings = boundedUnique([
      ...rawWarnings,
      ...transientErrors.map(() => "dashboard_refresh_retrying"),
    ]).filter((warning) => shouldShowDashboardWarning(warning, hasUsableData));
    dashboardFeedStatus.value = snapshot.status ?? {
      ...dashboardFeedStatus.value,
      running: true,
      market_open: Boolean(snapshot.session?.market_open),
      session_type: snapshot.session?.session_type ?? dashboardFeedStatus.value.session_type,
      session_phase: snapshot.session?.session_phase ?? dashboardFeedStatus.value.session_phase,
      venue_type: snapshot.session?.venue_type ?? dashboardFeedStatus.value.venue_type,
      last_refresh_at: snapshot.generated_at,
    };
    dashboardFeedStatus.value = {
      ...dashboardFeedStatus.value,
      next_session_at: snapshot.session?.next_session_at ?? dashboardFeedStatus.value.next_session_at,
      errors: blockingErrors,
      warnings: visibleWarnings,
    };
    feedErrors.value = blockingErrors;
    feedWarnings.value = visibleWarnings;
    warnings.value = visibleWarnings;
    if (snapshot.market_overview) {
      applyOverview(snapshot.market_overview);
      lastQuoteRefreshAt.value = snapshot.generated_at;
    } else if (snapshot.quote_rows?.length) {
      applyOverview({ generated_at: snapshot.generated_at, instruments: snapshot.quote_rows });
      lastQuoteRefreshAt.value = snapshot.generated_at;
    }
    if (snapshot.selected_details) {
      const selectedDetailsMatchesRequest =
        expectedSelectedInstrumentId === undefined ||
        snapshot.selected_details.instrument_id === expectedSelectedInstrumentId;
      const selectionStillCurrent =
        expectedSelectedInstrumentId === undefined ||
        selectedInstrumentId.value === expectedSelectedInstrumentId;
      if (selectedDetailsMatchesRequest && selectionStillCurrent) {
        selectedInstrumentId.value = snapshot.selected_details.instrument_id;
        lastDetailsRefreshAt.value = snapshot.generated_at;
      }
      applyOverview({ generated_at: snapshot.generated_at, instruments: [snapshot.selected_details] });
    }
    liveConnection.value = feedErrors.value.length ? "degraded" : "live";
  }

  function hasUsableDashboardFeedData(): boolean {
    if (
      dashboardFeedStatus.value.running &&
      isRecentIsoTimestamp(dashboardFeedStatus.value.last_refresh_at, 60_000)
    ) {
      return true;
    }
    return hasUsableDashboardSnapshotRows(overview.value.instruments);
  }

  function applyOverview(nextOverview: MarketOverviewResponse): void {
    if (!nextOverview.instruments.length) {
      recordWarning("empty_market_ws_snapshot");
      return;
    }
    error.value = null;
    const previousByInstrument = new Map(
      overview.value.instruments.map((instrument) => [instrument.instrument_id, instrument]),
    );
    const mergedByInstrument = new Map(
      ensureCoreRows(overview.value.instruments).map((instrument) => [
        instrument.instrument_id,
        instrument,
      ]),
    );
    for (const nextInstrument of nextOverview.instruments) {
      mergedByInstrument.set(
        nextInstrument.instrument_id,
        mergeInstrumentOverview(previousByInstrument.get(nextInstrument.instrument_id), nextInstrument),
      );
    }
    overview.value = {
      ...nextOverview,
      instruments: sortInstrumentRows(ensureCoreRows([...mergedByInstrument.values()])),
    };
    ensureSelectedInstrument();
  }

  async function fetchDataShadowStatus(): Promise<void> {
    if (dataShadowStatusInFlight) {
      return;
    }
    dataShadowStatusInFlight = true;
    try {
      dataShadowStatus.value = await apiClient.dataShadowStatus();
      clearWarning("data_shadow_status_unavailable");
    } catch (unknownError) {
      recordWarning("data_shadow_status_unavailable");
      if (overview.value.instruments.length === 0) {
        error.value = unknownError instanceof Error ? unknownError.message : "Data shadow status failed";
      }
    } finally {
      dataShadowStatusInFlight = false;
    }
  }

  function markDataShadowStopping(): void {
    dataShadowStatus.value = {
      ...dataShadowStatus.value,
      collector_state: "stopping",
      data_shadow_collector_state: "stopping",
      current_window_state: "stopping",
      effective_logging_state: "stopping",
      command_status: "stop_requested",
      preflight_phase: null,
      start_in_progress: false,
      daily_collection_active: false,
      collector_left_running: false,
      reason_code: "controlled_stop_requested",
      stream_alive: false,
      last_command_status: "stop_requested",
      last_command_reason_code: "controlled_stop_requested",
      warnings: dataShadowStatus.value.warnings.filter(
        (warning) => warning !== "collector_no_recent_samples",
      ),
    };
  }

  function markDataShadowStopped(commandId?: string | null): void {
    const stoppedAt = new Date().toISOString();
    dataShadowStatus.value = {
      ...dataShadowStatus.value,
      collector_state: "stopped_by_operator",
      data_shadow_collector_state: "stopped_by_operator",
      day_collection_state: "cancelled_by_operator",
      daily_collection_active: false,
      current_window_state: "stopped_by_operator",
      effective_logging_state: "stopped",
      command_status: "applied",
      preflight_phase: null,
      start_in_progress: false,
      collector_left_running: false,
      reason_code: "data_only_collection_stopped",
      stream_alive: false,
      stopped_at: stoppedAt,
      last_stop_reason: "data_only_collection_stopped",
      last_command_id: commandId ?? dataShadowStatus.value.last_command_id,
      last_command_status: "applied",
      last_command_reason_code: "data_only_collection_stopped",
      warnings: dataShadowStatus.value.warnings.filter(
        (warning) => warning !== "collector_no_recent_samples",
      ),
    };
  }

  async function connectMarketSocket(): Promise<void> {
    if (marketSocket && marketSocket.readyState < WebSocket.CLOSING) {
      return;
    }
    liveConnection.value = "loading";
    try {
      const requestedInstrumentId = selectedInstrumentId.value ?? DEFAULT_SELECTED_INSTRUMENT_ID;
      const query = new URLSearchParams({
        selected_instrument: requestedInstrumentId,
        include_order_book: "true",
        include_trades: "true",
      });
      marketSocket = await openAuthenticatedWebSocket(`/ws/market-feed?${query.toString()}`);
    } catch (unknownError) {
      error.value = unknownError instanceof Error ? unknownError.message : "Market WS auth failed";
      liveConnection.value = "degraded";
      return;
    }
    marketSocket.onopen = () => {
      liveConnection.value = "live";
    };
    marketSocket.onmessage = (event: MessageEvent<string>) => {
      try {
        const envelope = JSON.parse(event.data) as WebSocketEnvelope<{
          data?: DashboardMarketFeedSnapshot | MarketOverviewResponse;
        }>;
        const payload = envelope.payload.data;
        if (!payload) {
          return;
        }
        if ("quote_rows" in payload || "selected_details" in payload) {
          const expected = selectedInstrumentId.value ?? DEFAULT_SELECTED_INSTRUMENT_ID;
          applyDashboardFeedSnapshot(payload as DashboardMarketFeedSnapshot, expected);
        } else {
          applyOverview(payload as MarketOverviewResponse);
        }
      } catch {
        recordWarning("invalid_market_ws_snapshot");
      }
    };
    marketSocket.onerror = () => {
      liveConnection.value = "degraded";
    };
    marketSocket.onclose = () => {
      liveConnection.value = liveConnection.value === "degraded" ? "degraded" : "snapshot_closed";
      marketSocket = null;
    };
  }

  function startDashboardFeed(
    intervalMs = 3_000,
    selectedActiveIntervalMs = 10_000,
    selectedIdleIntervalMs = 15_000,
  ): void {
    if (marketPollTimer !== null) {
      return;
    }
    void connectMarketSocket();
    void fetchDashboardFeedSnapshot({ silent: true, includeOrderBook: false, includeTrades: false });
    void refreshSelectedInstrumentDetails();
    void fetchDataShadowStatus();
    marketPollTimer = window.setInterval(() => {
      void fetchDashboardFeedSnapshot({
        silent: true,
        includeOrderBook: false,
        includeTrades: false,
      });
      void fetchDataShadowStatus();
    }, intervalMs);
    selectedDetailsTimer = window.setInterval(() => {
      const targetInterval = dashboardFeedStatus.value.market_open
        ? selectedActiveIntervalMs
        : selectedIdleIntervalMs;
      if (Date.now() - lastSelectedDetailsFetchAt >= targetInterval) {
        void refreshSelectedInstrumentDetails();
      }
    }, 1_000);
  }

  function stopDashboardFeed(): void {
    if (marketPollTimer === null) {
      return;
    }
    window.clearInterval(marketPollTimer);
    marketPollTimer = null;
    if (quoteRefreshTimer !== null) {
      window.clearInterval(quoteRefreshTimer);
      quoteRefreshTimer = null;
    }
    if (selectedDetailsTimer !== null) {
      window.clearInterval(selectedDetailsTimer);
      selectedDetailsTimer = null;
    }
    clearSelectedOrderBookRetry();
  }

  function startMarketPolling(
    intervalMs = 5_000,
    selectedActiveIntervalMs = 10_000,
    selectedIdleIntervalMs = 15_000,
  ): void {
    startDashboardFeed(intervalMs, selectedActiveIntervalMs, selectedIdleIntervalMs);
  }

  function stopMarketPolling(): void {
    stopDashboardFeed();
  }

  watch(selectedInstrumentId, () => {
    selectedOrderBookRetryCount = 0;
    clearSelectedOrderBookRetry();
    sendMarketSelection();
    void refreshSelectedInstrumentDetails();
  });

  function sendMarketSelection(): void {
    if (!marketSocket || marketSocket.readyState !== WebSocket.OPEN) {
      return;
    }
    const instrumentId = selectedInstrumentId.value ?? DEFAULT_SELECTED_INSTRUMENT_ID;
    marketSocket.send(
      JSON.stringify({
        type: "market.select",
        selected_instrument: instrumentId,
      }),
    );
  }

  function ensureSelectedInstrument(): void {
    const rows = overview.value.instruments;
    if (rows.some((instrument) => instrument.instrument_id === selectedInstrumentId.value)) {
      return;
    }
    const defaultInstrument = rows.find(
      (instrument) => instrument.instrument_id === DEFAULT_SELECTED_INSTRUMENT_ID,
    );
    selectedInstrumentId.value = defaultInstrument?.instrument_id ?? rows[0]?.instrument_id ?? null;
  }

  function recordWarning(warning: string): void {
    warnings.value = Array.from(new Set([warning, ...warnings.value])).slice(0, 8);
  }

  function clearWarning(warning: string): void {
    warnings.value = warnings.value.filter((item) => item !== warning);
  }

  return {
    overview,
    dataShadowStatus,
    dashboardFeedStatus,
    selectedInstrumentId,
    quoteBoardLoading,
    selectedDetailsLoading,
    feedErrors,
    feedWarnings,
    lastQuoteRefreshAt,
    lastDetailsRefreshAt,
    loading,
    error,
    warnings,
    liveConnection,
    currentInstrument,
    quoteRows,
    topOfBook,
    bookSummaryRows,
    recentTrades,
    fetchOverview,
    refreshQuotes,
    fetchDashboardFeedSnapshot,
    refreshDashboardFeed,
    refreshSelectedInstrumentDetails,
    applyDashboardFeedSnapshot,
    applyOverview,
    fetchDataShadowStatus,
    markDataShadowStopping,
    markDataShadowStopped,
    connectMarketSocket,
    startDashboardFeed,
    stopDashboardFeed,
    startMarketPolling,
    stopMarketPolling,
  };
});

function emptyInstrument(instrumentId: string): MarketInstrumentOverview {
  const ticker = instrumentId.replace(/^MOEX:/, "");
  return {
    instrument_id: instrumentId,
    ticker,
    class_code: "TQBR",
    board: "TQBR",
    exchange: "MOEX",
    venue_type: "unknown",
    trading_mode: "unknown",
    official_exchange_open: false,
    official_exchange_closed: false,
    quote_source: "unavailable",
    quote_allowed_for_data_collection: false,
    quote_allowed_for_display: false,
    last_price: null,
    last_price_at: null,
    last_price_ts: null,
    last_price_source: null,
    is_price_stale: true,
    price_staleness_seconds: null,
    received_ts: null,
    exchange_ts: null,
    received_age_ms: null,
    exchange_age_ms: null,
    stale_by_received_time: true,
    stale_by_exchange_time: true,
    freshness_status: "unknown",
    freshness_reason: "instrument_unavailable",
    previous_close: null,
    change_abs: null,
    change_bps: null,
    session_type: null,
    broker_trading_status: null,
    api_trade_available: null,
    quote_status: "unavailable",
    last_candle_timeframe: null,
    spread: null,
    spread_abs: null,
    spread_bps: null,
    spread_abs_rub: null,
    spread_units_validated: true,
    mid_price: null,
    market_quality: null,
    market_quality_score: null,
    display_market_quality_score: null,
    calibration_market_quality_score: null,
    market_quality_label: "no_order_book_samples",
    market_quality_components: {
      reason_codes: ["instrument_unavailable", "no_order_book_samples"],
    },
    best_bid: null,
    best_ask: null,
    bid_depth_lots: null,
    ask_depth_lots: null,
    book_imbalance: null,
    order_book_source: null,
    order_book_ts: null,
    order_book_age_ms: null,
    order_book_stale: true,
    recent_market_trades: [],
    market_trades_source: "no_market_trades_samples",
    market_trades_age_ms: null,
    trade_tape_source: "no_market_trades_samples",
    trade_tape_status: "no_market_trades_samples",
    trade_tape_reason: "no_market_trades_samples",
    persisted_trade_tape_available: false,
    latest_persisted_trade_ts: null,
    dashboard_trade_tape_fallback: null,
    reason_code: "instrument_unavailable",
    warning: null,
    order_book_summary: {},
    quote_payload: {
      source: "unavailable",
      reason_code: "instrument_unavailable",
    },
  };
}

function ensureCoreRows(instruments: MarketInstrumentOverview[]): MarketInstrumentOverview[] {
  const byInstrument = new Map(instruments.map((instrument) => [instrument.instrument_id, instrument]));
  for (const instrumentId of CORE_INSTRUMENT_IDS) {
    if (!byInstrument.has(instrumentId)) {
      byInstrument.set(instrumentId, emptyInstrument(instrumentId));
    }
  }
  return [...byInstrument.values()];
}

function sortInstrumentRows(instruments: MarketInstrumentOverview[]): MarketInstrumentOverview[] {
  return [...instruments].sort((left, right) => {
    const leftOrder = CORE_INSTRUMENT_ORDER.get(left.instrument_id) ?? Number.MAX_SAFE_INTEGER;
    const rightOrder = CORE_INSTRUMENT_ORDER.get(right.instrument_id) ?? Number.MAX_SAFE_INTEGER;
    if (leftOrder !== rightOrder) {
      return leftOrder - rightOrder;
    }
    return left.instrument_id.localeCompare(right.instrument_id);
  });
}

function mergeInstrumentOverview(
  previous: MarketInstrumentOverview | undefined,
  next: MarketInstrumentOverview,
): MarketInstrumentOverview {
  if (!previous) {
    return next;
  }
  if (shouldKeepPreviousQuote(previous, next)) {
    return {
      ...next,
      ...quoteSnapshotFields(previous),
    };
  }
  return withPreservedRecentTrades(previous, withPreservedOrderBook(previous, next));
}

function withPreservedOrderBook(
  previous: MarketInstrumentOverview,
  next: MarketInstrumentOverview,
): MarketInstrumentOverview {
  if (!isOrderBookStillFresh(previous) || !canPreserveOrderBook(previous, next)) {
    return next;
  }
  const previousLevels = orderBookLevelCount(previous);
  if (previousLevels === 0) {
    return next;
  }
  const nextLevels = orderBookLevelCount(next);
  if (!next.order_book_stale && nextLevels >= previousLevels) {
    return next;
  }
  return {
    ...next,
    ...orderBookSnapshotFields(previous),
  };
}

function withPreservedRecentTrades(
  previous: MarketInstrumentOverview,
  next: MarketInstrumentOverview,
): MarketInstrumentOverview {
  if ((next.recent_market_trades?.length ?? 0) > 0) {
    return next;
  }
  if ((previous.recent_market_trades?.length ?? 0) === 0) {
    return next;
  }
  if (!isTradeTapeStillFresh(previous)) {
    return next;
  }
  return {
    ...next,
    recent_market_trades: previous.recent_market_trades,
    market_trades_source: previous.market_trades_source,
    market_trades_age_ms: tradeTapeAgeMs(previous) ?? previous.market_trades_age_ms,
    trade_tape_source: previous.trade_tape_source,
    trade_tape_status: previous.trade_tape_status,
    trade_tape_reason: previous.trade_tape_reason,
    persisted_trade_tape_available: previous.persisted_trade_tape_available,
    latest_persisted_trade_ts: previous.latest_persisted_trade_ts,
    dashboard_trade_tape_fallback: previous.dashboard_trade_tape_fallback,
  };
}

function isTradeTapeStillFresh(instrument: MarketInstrumentOverview): boolean {
  const ageMs = tradeTapeAgeMs(instrument);
  if (
    instrument.trade_tape_status === "stale" &&
    instrument.trade_tape_reason === "trade_exchange_ts_too_old" &&
    instrument.market_trades_source === "tbank_get_last_trades"
  ) {
    return ageMs !== null && ageMs <= DELAYED_TRADE_TAPE_DISPLAY_MAX_AGE_MS;
  }
  if (instrument.trade_tape_status && !["live", "fresh"].includes(instrument.trade_tape_status)) {
    return false;
  }
  if (ageMs !== null) {
    return ageMs <= LIVE_TRADE_TAPE_MAX_AGE_MS;
  }
  if (instrument.market_trades_age_ms === null || instrument.market_trades_age_ms === undefined) {
    return false;
  }
  return Number(instrument.market_trades_age_ms) <= LIVE_TRADE_TAPE_MAX_AGE_MS;
}

function tradeTapeAgeMs(instrument: MarketInstrumentOverview): number | null {
  let newest = Number.NEGATIVE_INFINITY;
  for (const trade of instrument.recent_market_trades ?? []) {
    const ts = tradeTimestamp(trade);
    if (ts !== null && ts > newest) {
      newest = ts;
    }
  }
  return Number.isFinite(newest) ? Math.max(0, Date.now() - newest) : null;
}

function tradeTimestamp(trade: JsonPayload): number | null {
  const raw = trade.exchange_ts ?? trade.ts_utc ?? trade.time ?? trade.ts;
  if (typeof raw !== "string") {
    return null;
  }
  const parsed = Date.parse(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

function isOrderBookStillFresh(instrument: MarketInstrumentOverview): boolean {
  if (!instrument.order_book_source || instrument.order_book_stale) {
    return false;
  }
  if (instrument.order_book_ts) {
    const parsed = Date.parse(instrument.order_book_ts);
    return Number.isFinite(parsed) && Date.now() - parsed <= 30_000;
  }
  if (instrument.order_book_age_ms !== null && instrument.order_book_age_ms !== undefined) {
    return Number(instrument.order_book_age_ms) <= 30_000;
  }
  return false;
}

function canPreserveOrderBook(
  previous: MarketInstrumentOverview,
  next: MarketInstrumentOverview,
): boolean {
  const previousSource = String(previous.order_book_source ?? previous.quote_source ?? "");
  if (
    next.official_exchange_open === false &&
    (previous.quote_allowed_for_data_collection ||
      previous.official_exchange_open ||
      previousSource.startsWith("live"))
  ) {
    return false;
  }
  return true;
}

function orderBookLevelCount(instrument: MarketInstrumentOverview): number {
  const summary = instrument.order_book_summary ?? {};
  const bids = Array.isArray(summary.bids) ? summary.bids.length : 0;
  const asks = Array.isArray(summary.asks) ? summary.asks.length : 0;
  return bids + asks;
}

function hasDisplayedSelectedOrderBook(instrument: MarketInstrumentOverview | null): boolean {
  const summary = instrument?.order_book_summary ?? {};
  const bids = Array.isArray(summary.bids) ? summary.bids.length : 0;
  const asks = Array.isArray(summary.asks) ? summary.asks.length : 0;
  return bids >= SELECTED_ORDER_BOOK_MIN_SIDE_LEVELS && asks >= SELECTED_ORDER_BOOK_MIN_SIDE_LEVELS;
}

function shouldRetrySelectedOrderBook(instrument: MarketInstrumentOverview | null): boolean {
  if (!instrument || !instrument.official_exchange_open) {
    return false;
  }
  if (instrument.order_book_stale) {
    return true;
  }
  return !hasDisplayedSelectedOrderBook(instrument);
}

function orderBookSnapshotFields(
  instrument: MarketInstrumentOverview,
): Partial<MarketInstrumentOverview> {
  return {
    spread: instrument.spread,
    spread_abs: instrument.spread_abs,
    spread_bps: instrument.spread_bps,
    spread_abs_rub: instrument.spread_abs_rub,
    spread_units_validated: instrument.spread_units_validated,
    mid_price: instrument.mid_price,
    market_quality: instrument.market_quality,
    market_quality_score: instrument.market_quality_score,
    display_market_quality_score: instrument.display_market_quality_score,
    calibration_market_quality_score: instrument.calibration_market_quality_score,
    market_quality_label: instrument.market_quality_label,
    market_quality_components: instrument.market_quality_components,
    best_bid: instrument.best_bid,
    best_ask: instrument.best_ask,
    bid_depth_lots: instrument.bid_depth_lots,
    ask_depth_lots: instrument.ask_depth_lots,
    book_imbalance: instrument.book_imbalance,
    order_book_source: instrument.order_book_source,
    order_book_ts: instrument.order_book_ts,
    order_book_age_ms: instrument.order_book_age_ms,
    order_book_stale: instrument.order_book_stale,
    order_book_summary: instrument.order_book_summary,
  };
}

function quoteSnapshotFields(instrument: MarketInstrumentOverview): Partial<MarketInstrumentOverview> {
  return {
    last_price: instrument.last_price,
    last_price_at: instrument.last_price_at,
    last_price_ts: instrument.last_price_ts,
    last_price_source: instrument.last_price_source,
    is_price_stale: instrument.is_price_stale,
    price_staleness_seconds: instrument.price_staleness_seconds,
    received_ts: instrument.received_ts,
    exchange_ts: instrument.exchange_ts,
    received_age_ms: instrument.received_age_ms,
    exchange_age_ms: instrument.exchange_age_ms,
    stale_by_received_time: instrument.stale_by_received_time,
    stale_by_exchange_time: instrument.stale_by_exchange_time,
    freshness_status: instrument.freshness_status,
    freshness_reason: instrument.freshness_reason,
    previous_close: instrument.previous_close,
    change_abs: instrument.change_abs,
    change_bps: instrument.change_bps,
    quote_status: instrument.quote_status,
    quote_source: instrument.quote_source,
    quote_allowed_for_data_collection: instrument.quote_allowed_for_data_collection,
    quote_allowed_for_display: instrument.quote_allowed_for_display,
    venue_type: instrument.venue_type,
    trading_mode: instrument.trading_mode,
    official_exchange_open: instrument.official_exchange_open,
    official_exchange_closed: instrument.official_exchange_closed,
    last_candle_timeframe: instrument.last_candle_timeframe,
    spread: instrument.spread,
    spread_abs: instrument.spread_abs,
    spread_bps: instrument.spread_bps,
    spread_abs_rub: instrument.spread_abs_rub,
    spread_units_validated: instrument.spread_units_validated,
    mid_price: instrument.mid_price,
    market_quality: instrument.market_quality,
    market_quality_score: instrument.market_quality_score,
    display_market_quality_score: instrument.display_market_quality_score,
    calibration_market_quality_score: instrument.calibration_market_quality_score,
    market_quality_label: instrument.market_quality_label,
    market_quality_components: instrument.market_quality_components,
    best_bid: instrument.best_bid,
    best_ask: instrument.best_ask,
    bid_depth_lots: instrument.bid_depth_lots,
    ask_depth_lots: instrument.ask_depth_lots,
    book_imbalance: instrument.book_imbalance,
    order_book_source: instrument.order_book_source,
    order_book_ts: instrument.order_book_ts,
    order_book_age_ms: instrument.order_book_age_ms,
    order_book_stale: instrument.order_book_stale,
    recent_market_trades: instrument.recent_market_trades,
    market_trades_source: instrument.market_trades_source,
    market_trades_age_ms: instrument.market_trades_age_ms,
    trade_tape_source: instrument.trade_tape_source,
    trade_tape_status: instrument.trade_tape_status,
    trade_tape_reason: instrument.trade_tape_reason,
    persisted_trade_tape_available: instrument.persisted_trade_tape_available,
    latest_persisted_trade_ts: instrument.latest_persisted_trade_ts,
    dashboard_trade_tape_fallback: instrument.dashboard_trade_tape_fallback,
    reason_code: instrument.reason_code,
    warning: instrument.warning,
    order_book_summary: instrument.order_book_summary,
    quote_payload: instrument.quote_payload,
  };
}

function shouldKeepPreviousQuote(
  previous: MarketInstrumentOverview,
  next: MarketInstrumentOverview,
): boolean {
  if (!previous.last_price) {
    return false;
  }
  if (next.official_exchange_closed !== previous.official_exchange_closed) {
    return false;
  }
  if (next.quote_source === "broker_quote_exchange_closed") {
    return false;
  }
  if (!isQuoteSnapshotStillFresh(previous)) {
    return false;
  }
  if (!next.last_price) {
    return true;
  }
  const previousPriority = quoteSourcePriority(previous.last_price_source);
  const nextPriority = quoteSourcePriority(next.last_price_source);
  if (previousPriority > nextPriority) {
    return true;
  }
  if (previousPriority < nextPriority) {
    return false;
  }
  return quoteTimestamp(previous.last_price_at) > quoteTimestamp(next.last_price_at);
}

function isQuoteSnapshotStillFresh(instrument: MarketInstrumentOverview): boolean {
  if (instrument.is_price_stale || instrument.stale_by_exchange_time) {
    return false;
  }
  if (instrument.exchange_age_ms !== null && instrument.exchange_age_ms !== undefined) {
    return Number(instrument.exchange_age_ms) <= 60_000;
  }
  if (
    ![
      "live_order_book_mid",
      "tbank_last_price",
      "live_exchange_order_book",
      "live_exchange_last_price",
      "broker_quote_exchange_closed",
      "broker_otc_order_book",
      "broker_indicative_quote",
    ].includes(instrument.last_price_source ?? "")
  ) {
    return true;
  }
  return Date.now() - quoteTimestamp(instrument.last_price_at) <= 60_000;
}

function quoteSourcePriority(source: string | null): number {
  if (source === "live_exchange_order_book" || source === "live_order_book_mid") {
    return 6;
  }
  if (source === "live_exchange_last_price") {
    return 5;
  }
  if (source === "broker_quote_exchange_closed" || source === "broker_otc_order_book") {
    return 4;
  }
  if (source === "broker_indicative_quote" || source === "tbank_last_price") {
    return 3;
  }
  if (source === "latest_market_candle_close") {
    return 1;
  }
  if (source === "previous_close") {
    return 0;
  }
  return 0;
}

function quoteTimestamp(value: string | null): number {
  if (!value) {
    return 0;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function isTransientDashboardFeedError(value: string): boolean {
  return TRANSIENT_DASHBOARD_FEED_ERRORS.has(value);
}

function shouldShowDashboardWarning(warning: string, hasUsableData: boolean): boolean {
  if (
    hasUsableData &&
    ["dashboard_refresh_in_progress", "no_market_trades_samples"].includes(warning)
  ) {
    return false;
  }
  return true;
}

function hasUsableInstrumentData(instrument: MarketInstrumentOverview): boolean {
  return Boolean(
    instrument.quote_status === "live" ||
      instrument.freshness_status === "fresh" ||
      instrument.order_book_stale === false ||
      (instrument.last_price && instrument.is_price_stale === false) ||
      instrument.best_bid ||
      instrument.best_ask,
  );
}

function hasUsableDashboardSnapshotRows(rows: MarketInstrumentOverview[] | undefined): boolean {
  return Boolean(rows?.some(hasUsableInstrumentData));
}

function snapshotHasUsableDashboardFeedData(snapshot: DashboardMarketFeedSnapshot): boolean {
  return Boolean(
    hasUsableDashboardSnapshotRows(snapshot.quote_rows) ||
      hasUsableDashboardSnapshotRows(snapshot.market_overview?.instruments) ||
      (snapshot.selected_details && hasUsableInstrumentData(snapshot.selected_details)),
  );
}

function isRecentIsoTimestamp(value: string | null | undefined, maxAgeMs: number): boolean {
  if (!value) {
    return false;
  }
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) {
    return false;
  }
  return Date.now() - parsed <= maxAgeMs;
}

function boundedUnique(values: string[], limit = 8): string[] {
  return Array.from(new Set(values.filter(Boolean))).slice(0, limit);
}

function errorText(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}
