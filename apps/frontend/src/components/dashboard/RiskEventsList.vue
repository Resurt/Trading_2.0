<script setup lang="ts">
import type { SignalResponse } from "../../api/types";
import EmptyState from "../ui/EmptyState.vue";
import StatusPill from "../ui/StatusPill.vue";

defineProps<{
  signals: SignalResponse[];
}>();
</script>

<template>
  <div v-if="signals.length" class="event-list">
    <div v-for="signal in signals.slice(0, 6)" :key="signal.candidate_id" class="event-row">
      <div>
        <strong>{{ signal.instrument_id }}</strong>
        <span>{{ signal.timeframe }} · {{ signal.side }} · {{ signal.signal_type }}</span>
      </div>
      <StatusPill :code="signal.final_blocker_code ?? signal.candidate_status" compact />
    </div>
  </div>
  <EmptyState
    v-else
    title="Risk events не загружены"
    detail="После появления signal_candidate здесь будут видны blocker/candidate причины."
  />
</template>
