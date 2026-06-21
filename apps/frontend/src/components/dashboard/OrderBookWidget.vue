<script setup lang="ts">
import { computed } from "vue";

import type { MarketInstrumentOverview } from "../../api/types";
import { formatDecimal, stringifyValue } from "../../utils/format";
import EmptyState from "../ui/EmptyState.vue";

const props = defineProps<{
  instrument: MarketInstrumentOverview | null;
}>();

const bidQty = computed(() => props.instrument?.order_book_summary.best_bid_qty_lots ?? null);
const askQty = computed(() => props.instrument?.order_book_summary.best_ask_qty_lots ?? null);
const bidDepth = computed(() => props.instrument?.order_book_summary.bid_depth_lots ?? null);
const askDepth = computed(() => props.instrument?.order_book_summary.ask_depth_lots ?? null);
const hasBook = computed(
  () =>
    props.instrument?.best_bid !== null &&
    props.instrument?.best_bid !== undefined &&
    props.instrument?.best_ask !== null &&
    props.instrument?.best_ask !== undefined,
);
</script>

<template>
  <div v-if="instrument && hasBook" class="order-book-widget">
    <div class="book-side book-side--bid">
      <span>Bid</span>
      <strong>{{ formatDecimal(instrument.best_bid, 2) }}</strong>
      <small>{{ stringifyValue(bidQty) }} lots</small>
      <div class="book-depth">
        <i />
        <span>{{ stringifyValue(bidDepth) }}</span>
      </div>
    </div>
    <div class="book-mid">
      <span>Mid</span>
      <strong>{{ formatDecimal(instrument.mid_price, 2) }}</strong>
      <small>spread {{ formatDecimal(instrument.spread, 4) }}</small>
    </div>
    <div class="book-side book-side--ask">
      <span>Ask</span>
      <strong>{{ formatDecimal(instrument.best_ask, 2) }}</strong>
      <small>{{ stringifyValue(askQty) }} lots</small>
      <div class="book-depth">
        <i />
        <span>{{ stringifyValue(askDepth) }}</span>
      </div>
    </div>
  </div>
  <EmptyState
    v-else
    title="Стакан пока не получен"
    detail="Показывается последняя цена. Bid, ask, mid, spread и качество появятся после успешного read-only GetOrderBook или live data-only потока."
    tone="warn"
  />
</template>
