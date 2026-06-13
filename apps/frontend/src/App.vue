<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";

import { useShellStore } from "./stores/shell";

const shell = useShellStore();
</script>

<template>
  <div class="app-shell">
    <header class="top-bar">
      <div>
        <strong>Trading 2.0</strong>
        <span class="muted">MOEX / T-Invest</span>
      </div>
      <div class="status-strip" aria-label="service status">
        <span class="status-dot" />
        <span>{{ shell.runtimeMode }}</span>
        <span>{{ shell.sessionType }}</span>
        <span>{{ shell.sessionPhase }}</span>
      </div>
    </header>

    <aside class="side-nav" aria-label="main navigation">
      <RouterLink to="/">Live Dashboard</RouterLink>
      <RouterLink to="/reports">Reports</RouterLink>
      <RouterLink to="/settings">Settings</RouterLink>
      <RouterLink to="/diagnostics">Logs/Diagnostics</RouterLink>
    </aside>

    <main class="main-panel">
      <RouterView />
    </main>
  </div>
</template>
