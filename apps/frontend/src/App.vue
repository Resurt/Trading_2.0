<script setup lang="ts">
import { onMounted } from "vue";
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

import StatusPill from "./components/ui/StatusPill.vue";
import { useMarketStore } from "./stores/market";
import { usePortfolioStore } from "./stores/portfolio";
import { useReportsStore } from "./stores/reports";
import { useRobotStore } from "./stores/robot";
import { compactDateTime, countdownFromMicroSession } from "./utils/format";

const robot = useRobotStore();
const market = useMarketStore();
const portfolio = usePortfolioStore();
const reports = useReportsStore();

const navItems = [
  { to: "/", label: "Live Dashboard", icon: LayoutDashboard },
  { to: "/reports", label: "Reports", icon: BarChart3 },
  { to: "/intraday", label: "Intraday", icon: Clock3 },
  { to: "/historical", label: "Historical Data", icon: Database },
  { to: "/calibration", label: "Calibration", icon: LineChart },
  { to: "/settings", label: "Settings", icon: Settings },
  { to: "/diagnostics", label: "Logs/Diagnostics", icon: FileTerminal },
];

onMounted(() => {
  void Promise.allSettled([
    robot.fetchInitialSnapshot(),
    market.fetchOverview(),
    market.fetchDataShadowStatus(),
    portfolio.fetchSnapshot(),
    reports.fetchReports(),
  ]);
  void robot.connectDashboardSocket();
  void market.connectMarketSocket();
  void portfolio.connectOrdersSocket();
  void reports.connectReportsSocket();
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
        <StatusPill :code="robot.status.session_type" compact />
        <StatusPill :code="robot.status.session_phase" compact />
        <StatusPill :code="robot.status.broker_trading_status" compact />
        <span class="micro-countdown">
          {{ robot.status.micro_session_id ?? "no_micro_session" }}
          <b>{{ countdownFromMicroSession(robot.status.micro_session_id) }}</b>
        </span>
        <span v-if="robot.lastCommandStatus" class="command-result" data-testid="command-result">
          <StatusPill :code="robot.lastCommandStatus" compact />
          <span>{{ robot.lastCommandMessage }}</span>
          <code v-if="robot.lastCommandReasonCode">{{ robot.lastCommandReasonCode }}</code>
          <small v-if="robot.lastCommandNextSessionAt">
            next {{ compactDateTime(robot.lastCommandNextSessionAt) }}
          </small>
        </span>
      </div>

      <div class="top-actions">
        <span class="last-sync">{{ compactDateTime(robot.lastDashboardMessageAt) }}</span>
        <button
          class="icon-button icon-button--good"
          title="Запросить запуск data-only сбора"
          type="button"
          :disabled="robot.commandLoading"
          @click="robot.startRobot"
        >
          <CirclePlay :size="18" aria-hidden="true" />
          <span>{{ robot.commandLoading ? "Wait" : "Start" }}</span>
        </button>
        <button
          class="icon-button icon-button--danger"
          title="Запросить controlled stop"
          type="button"
          :disabled="robot.commandLoading"
          @click="robot.stopRobot"
        >
          <Square :size="16" aria-hidden="true" />
          <span>Stop</span>
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
