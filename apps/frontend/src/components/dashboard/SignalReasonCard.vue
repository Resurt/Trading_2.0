<script setup lang="ts">
import type { SignalResponse } from "../../api/types";
import { formatDecimal } from "../../utils/format";
import { codeWithLabel } from "../../utils/labels";
import EmptyState from "../ui/EmptyState.vue";
import StatusPill from "../ui/StatusPill.vue";

defineProps<{
  signal: SignalResponse | null;
}>();
</script>

<template>
  <div v-if="signal" class="reason-card">
    <div class="reason-card__top">
      <strong>{{ signal.instrument_id }}</strong>
      <StatusPill :code="signal.candidate_status" compact />
    </div>
    <dl class="definition-grid">
      <dt>candidate_id</dt>
      <dd><code>{{ signal.candidate_id }}</code></dd>
      <dt>timeframe</dt>
      <dd>{{ signal.timeframe }}</dd>
      <dt>side</dt>
      <dd>{{ signal.side }}</dd>
      <dt>signal_type</dt>
      <dd>{{ signal.signal_type }}</dd>
      <dt>expected_edge_bps</dt>
      <dd>{{ formatDecimal(signal.expected_edge_bps, 2) }}</dd>
      <dt>final_blocker</dt>
      <dd>
        <StatusPill v-if="signal.final_blocker_code" :code="signal.final_blocker_code" />
        <span v-else>Нет финального блокера</span>
      </dd>
    </dl>
    <p v-if="signal.final_blocker_code" class="reason-card__explain">
      Причина: {{ codeWithLabel(signal.final_blocker_code) }}.
    </p>
  </div>
  <EmptyState
    v-else
    title="Текущий кандидат отсутствует"
    detail="Нет активного signal_candidate в текущей micro-session."
  />
</template>
