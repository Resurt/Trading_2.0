<script setup lang="ts">
import { onMounted, onUnmounted } from "vue";
import { RouterLink, RouterView } from "vue-router";
import {
  Activity,
  BarChart3,
  CirclePlay,
  Clock3,
  Database,
  FileTerminal,
  LineChart,
  LayoutDashboard,
  Settings,
  Square,
} from "@lucide/vue";

import { apiClient } from "./api/client";
import { useMarketStore } from "./stores/market";
import { usePortfolioStore } from "./stores/portfolio";
import { useRobotStore } from "./stores/robot";
import { compactDateTime } from "./utils/format";

const robot = useRobotStore();
const market = useMarketStore();
const portfolio = usePortfolioStore();

const navItems = [
  { to: "/", label: "Live Dashboard", icon: LayoutDashboard },
  { to: "/reports", label: "Reports", icon: BarChart3 },
  { to: "/intraday", label: "Intraday", icon: Clock3 },
  { to: "/historical", label: "Historical Data", icon: Database },
  { to: "/calibration", label: "Calibration", icon: LineChart },
  { to: "/settings", label: "Settings", icon: Settings },
  { to: "/diagnostics", label: "Logs/Diagnostics", icon: FileTerminal },
];

function connectionText(label: string, state: string): string {
  const states: Record<string, string> = {
    live: "онлайн",
    loading: "подключение",
    idle: "ожидание",
    degraded: "нет связи",
    snapshot_closed: "snapshot",
  };
  return `${label}: ${states[state] ?? state}`;
}

function brokerConnectionState(): string {
  if ([robot.liveConnection, market.liveConnection, portfolio.liveConnection].includes("degraded")) {
    return "degraded";
  }
  if ([robot.liveConnection, market.liveConnection, portfolio.liveConnection].includes("loading")) {
    return "loading";
  }
  if ([robot.liveConnection, market.liveConnection, portfolio.liveConnection].includes("live")) {
    return "live";
  }
  return "idle";
}

function startButtonLabel(): string {
  if (!robot.startLoading) {
    return "Старт";
  }
  return robot.commandPhase === "preflight" ? "Проверка" : "Запуск";
}

async function bootstrapDashboard(): Promise<void> {
  try {
    const snapshot = await apiClient.dashboardState();
    robot.applyDashboardSnapshot(snapshot);
    if (snapshot.data?.market) {
      market.applyOverview(snapshot.data.market);
    }
    void market.refreshQuotes();
    portfolio.applySnapshot({
      positions: snapshot.data?.positions,
      open_orders: snapshot.data?.open_orders,
    });
  } catch {
    await Promise.allSettled([
      robot.fetchInitialSnapshot(),
      market.refreshQuotes(),
      market.fetchOverview({ silent: true }),
      portfolio.fetchSnapshot(),
    ]);
  }
}

onMounted(() => {
  void bootstrapDashboard();
  void market.fetchDataShadowStatus();
  void robot.connectDashboardSocket();
  void market.connectMarketSocket();
  void portfolio.connectOrdersSocket();
  robot.startBalancePolling();
  market.startMarketPolling();
});

onUnmounted(() => {
  robot.stopBalancePolling();
  market.stopMarketPolling();
});
</script>

<template>
  <div class="app-shell">
    <header class="top-bar">
      <div class="brand-mark">
        <Activity :size="20" aria-hidden="true" />
        <div>
          <strong>Trading 2.0</strong>
          <span>MOEX · T-Invest</span>
        </div>
      </div>

      <div class="status-strip" aria-label="service status">
        <span class="connection-chip" :class="`connection-chip--${brokerConnectionState()}`">
          <span class="connection-chip__dot" />
          {{ connectionText("Брокер/API", brokerConnectionState()) }}
        </span>
      </div>

      <div class="top-actions">
        <span class="last-sync">{{ compactDateTime(robot.lastDashboardMessageAt) }}</span>
        <button
          class="icon-button icon-button--good"
          :class="{ 'icon-button--working': robot.startLoading }"
          title="Запросить запуск data-only сбора"
          type="button"
          :disabled="robot.startLoading"
          @click="robot.startRobot"
        >
          <span v-if="robot.startLoading" class="button-spinner" aria-hidden="true" />
          <CirclePlay :size="18" aria-hidden="true" />
          <span>{{ startButtonLabel() }}</span>
        </button>
        <button
          class="icon-button icon-button--danger"
          :class="{ 'icon-button--working': robot.stopLoading }"
          title="Запросить controlled stop"
          type="button"
          :disabled="robot.stopLoading"
          @click="robot.stopRobot"
        >
          <span v-if="robot.stopLoading" class="button-spinner" aria-hidden="true" />
          <Square :size="16" aria-hidden="true" />
          <span>{{ robot.stopLoading ? "Стоп..." : "Стоп" }}</span>
        </button>
      </div>
    </header>

    <aside class="side-nav" aria-label="main navigation">
      <RouterLink v-for="item in navItems" :key="item.to" :to="item.to">
        <component :is="item.icon" :size="18" aria-hidden="true" />
        <span>{{ item.label }}</span>
      </RouterLink>
    </aside>

    <main class="main-panel">
      <RouterView />
    </main>
  </div>
</template>
