<script setup lang="ts">
import { computed } from "vue";

import type { JsonPayload, MarketInstrumentOverview } from "../../api/types";
import EmptyState from "../ui/EmptyState.vue";

type BookSide = "bid" | "ask";

interface BookLevel {
  price: number;
  quantity: number;
  priceText: string;
  quantityText: string;
}

const props = defineProps<{
  instrument: MarketInstrumentOverview | null;
}>();

const bids = computed(() =>
  levelsFromSummary("bids", "bid", props.instrument?.best_bid, props.instrument?.order_book_summary.best_bid_qty_lots),
);
const asks = computed(() =>
  levelsFromSummary("asks", "ask", props.instrument?.best_ask, props.instrument?.order_book_summary.best_ask_qty_lots),
);
const rowCount = computed(() => Math.max(bids.value.length, asks.value.length));
const rows = computed(() =>
  Array.from({ length: rowCount.value }, (_, index) => ({
    bid: bids.value[index] ?? null,
    ask: asks.value[index] ?? null,
  })),
);
const maxQuantity = computed(() =>
  Math.max(1, ...bids.value.map((level) => level.quantity), ...asks.value.map((level) => level.quantity)),
);
const hasBook = computed(() => bids.value.length > 0 || asks.value.length > 0);
const bookClock = computed(() => timeOnly(props.instrument?.order_book_ts ?? props.instrument?.last_price_at));
const bookSource = computed(() => {
  if (!props.instrument?.order_book_source) {
    return props.instrument?.last_price_source ?? "no_order_book";
  }
  return props.instrument.order_book_source;
});

function levelsFromSummary(
  key: "bids" | "asks",
  side: BookSide,
  fallbackPrice: string | null | undefined,
  fallbackQuantity: unknown,
): BookLevel[] {
  const raw = props.instrument?.order_book_summary[key];
  const parsed = Array.isArray(raw)
    ? raw
        .map((item) => levelFromPayload(item))
        .filter((level): level is BookLevel => level !== null)
    : [];
  if (parsed.length > 0) {
    return parsed.sort((left, right) =>
      side === "bid" ? right.price - left.price : left.price - right.price,
    );
  }
  const fallback = levelFromValues(fallbackPrice, fallbackQuantity);
  return fallback ? [fallback] : [];
}

function levelFromPayload(value: unknown): BookLevel | null {
  if (!isPayload(value)) {
    return null;
  }
  return levelFromValues(
    value.price ?? value.price_units ?? value.price_decimal,
    value.quantity_lots ?? value.quantity ?? value.qty_lots ?? value.lots,
  );
}

function levelFromValues(priceValue: unknown, quantityValue: unknown): BookLevel | null {
  const price = numeric(priceValue);
  const quantity = numeric(quantityValue);
  if (price === null || quantity === null) {
    return null;
  }
  return {
    price,
    quantity,
    priceText: formatPrice(price),
    quantityText: formatQuantity(quantity),
  };
}

function isPayload(value: unknown): value is JsonPayload {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function numeric(value: unknown): number | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatPrice(value: number): string {
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: value >= 1000 ? 2 : 4,
  }).format(value);
}

function formatQuantity(value: number): string {
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
  }).format(value);
}

function barWidth(level: BookLevel | null): string {
  if (!level) {
    return "0%";
  }
  return `${Math.max(2, Math.min(100, (level.quantity / maxQuantity.value) * 100))}%`;
}

function timeOnly(value: string | null | undefined): string {
  if (!value) {
    return "--:--:--";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "--:--:--";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(parsed);
}
</script>

<template>
  <section v-if="instrument && hasBook" class="depth-ladder-card">
    <header class="depth-ladder-header">
      <div>
        <h3>СТАКАН</h3>
        <span>{{ instrument.ticker ?? instrument.instrument_id }} · {{ bookSource }}</span>
      </div>
      <div class="depth-ladder-clock">
        <strong>{{ bookClock }}</strong>
        <small v-if="instrument.order_book_stale">stale</small>
        <small v-else>fresh</small>
      </div>
    </header>

    <div class="depth-ladder-grid depth-ladder-grid--head">
      <span>БИД</span>
      <span>ОБЪЕМ</span>
      <span>АСК</span>
      <span>ОБЪЕМ</span>
    </div>

    <div class="depth-ladder-rows">
      <div v-for="(row, index) in rows" :key="index" class="depth-ladder-grid depth-ladder-row">
        <span class="depth-price depth-price--bid">{{ row.bid?.priceText ?? "" }}</span>
        <span class="depth-volume depth-volume--bid">
          <i :style="{ width: barWidth(row.bid) }" />
          <strong>{{ row.bid?.quantityText ?? "" }}</strong>
        </span>
        <span class="depth-price depth-price--ask">{{ row.ask?.priceText ?? "" }}</span>
        <span class="depth-volume depth-volume--ask">
          <i :style="{ width: barWidth(row.ask) }" />
          <strong>{{ row.ask?.quantityText ?? "" }}</strong>
        </span>
      </div>
    </div>
  </section>

  <EmptyState
    v-else
    title="Стакан пока не получен"
    detail="Нет уровней bid/ask. Показываем последнюю цену; ladder появится после read-only GetOrderBook или live data-only потока."
    tone="warn"
  />
</template>
