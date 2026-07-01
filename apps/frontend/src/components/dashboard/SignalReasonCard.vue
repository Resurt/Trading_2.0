<script setup lang="ts">
import type { SignalResponse } from "../../api/types";
import { formatDecimal, stringifyValue } from "../../utils/format";
import { humanizeCode } from "../../utils/labels";
import EmptyState from "../ui/EmptyState.vue";
import StatusPill from "../ui/StatusPill.vue";

defineProps<{
  signal: SignalResponse | null;
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
    return "вход в позицию";
  }
  if (type === "exit") {
    return "выход из позиции";
  }
  return type ?? "нет типа сигнала";
}
</script>

<template>
  <div v-if="signal" class="reason-card">
    <div class="reason-card__top">
      <strong>{{ signal.instrument_id }}</strong>
      <StatusPill :code="signal.candidate_status" :label="humanizeCode(signal.candidate_status)" compact />
    </div>
    <dl class="definition-grid">
      <dt>ID кандидата</dt>
      <dd><code>{{ signal.candidate_id }}</code></dd>
      <dt>таймфрейм</dt>
      <dd>{{ signal.timeframe }}</dd>
      <dt>направление</dt>
      <dd>{{ sideLabel(signal.side) }}</dd>
      <dt>тип сигнала</dt>
      <dd>{{ signalTypeLabel(signal.signal_type) }}</dd>
      <dt>ожидаемый запас</dt>
      <dd>{{ formatDecimal(signal.expected_edge_bps, 2) }}</dd>
      <dt>почему не торгуем</dt>
      <dd>
        <StatusPill
          v-if="signal.final_blocker_code"
          :code="signal.final_blocker_code"
          :label="humanizeCode(signal.final_blocker_code)"
          compact
        />
        <span v-else>блокировки нет</span>
      </dd>
    </dl>
    <p v-if="signal.final_blocker_code" class="reason-card__explain">
      Причина блокировки: {{ humanizeCode(signal.final_blocker_code) }}.
      <span v-if="signal.payload.explanation || signal.payload.reason">
        {{ stringifyValue(signal.payload.explanation ?? signal.payload.reason) }}
      </span>
    </p>
  </div>
  <EmptyState
    v-else
    title="Активного сигнала нет"
    detail="В текущем часовом окне стратегия не сформировала сигнал для сделки. В data-only режиме это нормально: заявки не создаются."
  />
</template>
