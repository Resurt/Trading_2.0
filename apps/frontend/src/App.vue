<script setup lang="ts">
import { onMounted } from "vue";
import { RouterLink, RouterView } from "vue-router";
import {
  Activity,
  BarChart3,
  CirclePlay,
  FileTerminal,
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
  { to: "/settings", label: "Settings", icon: Settings },
  { to: "/diagnostics", label: "Logs/Diagnostics", icon: FileTerminal },
];

onMounted(() => {
  void Promise.allSettled([
    robot.fetchInitialSnapshot(),
    market.fetchOverview(),
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
      </div>

      <div class="top-actions">
        <span class="last-sync">{{ compactDateTime(robot.lastDashboardMessageAt) }}</span>
        <button class="icon-button icon-button--good" title="Запросить запуск" @click="robot.startRobot">
          <CirclePlay :size="18" aria-hidden="true" />
          <span>Start</span>
        </button>
        <button class="icon-button icon-button--danger" title="Controlled stop" @click="robot.stopRobot">
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
