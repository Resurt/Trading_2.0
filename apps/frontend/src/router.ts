import { createRouter, createWebHistory } from "vue-router";

import DiagnosticsView from "./views/DiagnosticsView.vue";
import CalibrationView from "./views/CalibrationView.vue";
import HistoricalDataView from "./views/HistoricalDataView.vue";
import LiveDashboardView from "./views/LiveDashboardView.vue";
import ReportsView from "./views/ReportsView.vue";
import SettingsView from "./views/SettingsView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", name: "live-dashboard", component: LiveDashboardView },
    { path: "/reports", name: "reports", component: ReportsView },
    { path: "/historical", name: "historical-data", component: HistoricalDataView },
    { path: "/calibration", name: "calibration", component: CalibrationView },
    { path: "/settings", name: "settings", component: SettingsView },
    { path: "/diagnostics", name: "diagnostics", component: DiagnosticsView },
  ],
});
