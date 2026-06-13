import { createRouter, createWebHistory } from "vue-router";

import DiagnosticsView from "./views/DiagnosticsView.vue";
import LiveDashboardView from "./views/LiveDashboardView.vue";
import ReportsView from "./views/ReportsView.vue";
import SettingsView from "./views/SettingsView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", name: "live-dashboard", component: LiveDashboardView },
    { path: "/reports", name: "reports", component: ReportsView },
    { path: "/settings", name: "settings", component: SettingsView },
    { path: "/diagnostics", name: "diagnostics", component: DiagnosticsView },
  ],
});
