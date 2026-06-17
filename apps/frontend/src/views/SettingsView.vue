<script setup lang="ts">
import { onMounted, ref } from "vue";
import { RefreshCw, Save, ShieldCheck } from "@lucide/vue";

import { apiAuthMode, apiClient, runtimeMode } from "../api/client";
import type { AuthStatusResponse, StrategyConfigResponse } from "../api/types";
import DataPanel from "../components/ui/DataPanel.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import MetricTile from "../components/ui/MetricTile.vue";
import StatusPill from "../components/ui/StatusPill.vue";
import { useRobotStore } from "../stores/robot";
import { compactDateTime, stringifyValue } from "../utils/format";

const robot = useRobotStore();
const strategyId = ref("baseline");
const sessionTemplate = ref("weekday_main");
const config = ref<StrategyConfigResponse | null>(null);
const authStatus = ref<AuthStatusResponse | null>(null);
const error = ref<string | null>(null);
const authError = ref<string | null>(null);
const saving = ref(false);

async function loadConfig(): Promise<void> {
  error.value = null;
  try {
    config.value = await apiClient.strategyConfig(strategyId.value, sessionTemplate.value);
  } catch (unknownError) {
    error.value = unknownError instanceof Error ? unknownError.message : "Strategy config load failed";
    config.value = null;
  }
}

async function loadAuthStatus(): Promise<void> {
  authError.value = null;
  try {
    authStatus.value = await apiClient.authStatus();
  } catch (unknownError) {
    authError.value = unknownError instanceof Error ? unknownError.message : "Auth status load failed";
    authStatus.value = null;
  }
}

async function saveConfig(): Promise<void> {
  if (!config.value) {
    return;
  }
  saving.value = true;
  error.value = null;
  try {
    config.value = await apiClient.updateStrategyConfig({
      strategy_id: strategyId.value,
      session_template: sessionTemplate.value,
      config_payload: config.value.config_payload,
      risk_limits: config.value.risk_limits,
      actor: "frontend_operator",
    });
  } catch (unknownError) {
    error.value = unknownError instanceof Error ? unknownError.message : "Strategy config update failed";
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  void loadConfig();
  void loadAuthStatus();
});
</script>

<template>
  <section class="page-stack" data-testid="settings-page">
    <div class="page-heading">
      <h1>Settings</h1>
      <div class="heading-status">
        <StatusPill :code="robot.status.robot_control_state" />
        <StatusPill :code="robot.status.session_type" />
      </div>
    </div>

    <div class="metric-grid">
      <MetricTile label="Runtime mode" :value="runtimeMode" :code="runtimeMode" />
      <MetricTile
        label="Auth mode"
        :value="authStatus?.auth_mode ?? apiAuthMode"
        :detail="authStatus ? `${authStatus.role} / ${authStatus.subject}` : 'token value hidden'"
        :tone="authStatus?.production_like ? 'warn' : 'info'"
      />
      <MetricTile
        label="Active instruments"
        :value="robot.status.active_instruments.length"
        :detail="robot.status.active_instruments.join(', ') || 'Нет данных'"
      />
      <MetricTile
        label="Active timeframes"
        :value="robot.status.active_timeframes.length"
        :detail="robot.status.active_timeframes.join(', ') || 'Нет данных'"
      />
      <MetricTile label="Secret values" value="hidden" detail="status only; values are not rendered" tone="warn" />
    </div>

    <DataPanel>
      <template #eyebrow>control-plane auth</template>
      <template #title>Frontend auth status</template>
      <template #action>
        <button class="icon-button" type="button" @click="loadAuthStatus">
          <ShieldCheck :size="16" aria-hidden="true" />
          <span>Check</span>
        </button>
      </template>

      <EmptyState v-if="authError" title="Auth status degraded" :detail="authError" tone="warn" />
      <dl v-else-if="authStatus" class="definition-grid definition-grid--wide">
        <dt>auth_mode</dt>
        <dd><code>{{ authStatus.auth_mode }}</code></dd>
        <dt>role</dt>
        <dd><code>{{ authStatus.role }}</code></dd>
        <dt>subject</dt>
        <dd><code>{{ authStatus.subject }}</code></dd>
        <dt>production_like</dt>
        <dd>{{ authStatus.production_like }}</dd>
      </dl>
      <EmptyState
        v-else
        title="Auth status is loading"
        detail="Frontend never renders bearer token or websocket ticket values."
      />
    </DataPanel>

    <DataPanel>
      <template #eyebrow>strategy_config</template>
      <template #title>Session template config</template>
      <template #action>
        <button class="icon-button" type="button" @click="loadConfig">
          <RefreshCw :size="16" aria-hidden="true" />
          <span>Refresh</span>
        </button>
      </template>

      <form class="filter-grid" @submit.prevent="saveConfig">
        <label>
          <span>strategy_id</span>
          <input v-model="strategyId" />
        </label>
        <label>
          <span>session_template</span>
          <select v-model="sessionTemplate" @change="loadConfig">
            <option value="weekday_morning">weekday_morning</option>
            <option value="weekday_main">weekday_main</option>
            <option value="weekday_evening">weekday_evening</option>
            <option value="weekend">weekend</option>
          </select>
        </label>
        <div class="filter-actions">
          <button class="icon-button icon-button--good" type="submit" :disabled="saving || !config">
            <Save :size="16" aria-hidden="true" />
            <span>{{ saving ? "Saving" : "Save version" }}</span>
          </button>
        </div>
      </form>

      <EmptyState v-if="error" title="Strategy config degraded" :detail="error" tone="warn" />

      <dl v-if="config" class="definition-grid definition-grid--wide">
        <dt>strategy_config_id</dt>
        <dd><code>{{ config.strategy_config_id ?? "not_created" }}</code></dd>
        <dt>version</dt>
        <dd>{{ config.version }}</dd>
        <dt>is_active</dt>
        <dd>{{ config.is_active }}</dd>
        <dt>valid_from</dt>
        <dd>{{ compactDateTime(config.valid_from) }}</dd>
        <dt>valid_to</dt>
        <dd>{{ compactDateTime(config.valid_to) }}</dd>
      </dl>
      <EmptyState
        v-else-if="!error"
        title="Конфигурация не загружена"
        detail="BFF вернет version 0, если active config еще не создан."
      />
    </DataPanel>

    <div class="reports-grid">
      <DataPanel>
        <template #eyebrow>config_payload</template>
        <template #title>Strategy payload</template>
        <div v-if="config" class="json-lines">
          <div v-for="(value, key) in config.config_payload" :key="key">
            <span>{{ key }}</span>
            <code>{{ stringifyValue(value) }}</code>
          </div>
        </div>
        <EmptyState v-else title="Нет payload" />
      </DataPanel>

      <DataPanel>
        <template #eyebrow>risk_limits</template>
        <template #title>Risk limits</template>
        <div v-if="config" class="json-lines">
          <div v-for="(value, key) in config.risk_limits" :key="key">
            <span>{{ key }}</span>
            <code>{{ stringifyValue(value) }}</code>
          </div>
        </div>
        <EmptyState v-else title="Нет risk limits" />
      </DataPanel>
    </div>
  </section>
</template>
