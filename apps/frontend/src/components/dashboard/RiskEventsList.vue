<script setup lang="ts">
import type { SignalResponse } from "../../api/types";
import { humanizeCode } from "../../utils/labels";
import EmptyState from "../ui/EmptyState.vue";
import StatusPill from "../ui/StatusPill.vue";

defineProps<{
  signals: SignalResponse[];
}>();

function sideLabel(side: string | null | undefined): string {
  if (side === "buy") {
    return "покупка";
  }
  if (side === "sell") {
    return "продажа";
  }
  return side ?? "нет направления";
}

function signalTypeLabel(type: string | null | undefined): string {
  if (type === "entry") {
    return "вход";
  }
  if (type === "exit") {
    return "выход";
  }
  return type ?? "сигнал";
}
</script>

<template>
  <div v-if="signals.length" class="event-list">
    <div v-for="signal in signals.slice(0, 6)" :key="signal.candidate_id" class="event-row">
      <div>
        <strong>{{ signal.instrument_id }}</strong>
        <span>{{ signal.timeframe }} · {{ sideLabel(signal.side) }} · {{ signalTypeLabel(signal.signal_type) }}</span>
      </div>
      <StatusPill
        :code="signal.final_blocker_code ?? signal.candidate_status"
        :label="humanizeCode(signal.final_blocker_code ?? signal.candidate_status)"
        compact
      />
    </div>
  </div>
  <EmptyState
    v-else
    title="Блокировок стратегии нет"
    detail="Когда стратегия найдёт сигнал и отклонит сделку, здесь появится понятная причина: спред, качество рынка, риск-лимит или запрет сессии."
  />
</template>
