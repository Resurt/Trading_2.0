<script setup lang="ts">
defineProps<{
  label: string;
  value: string | number;
  code?: string | null;
  detail?: string;
  tone?: "neutral" | "good" | "warn" | "bad" | "info";
}>();
</script>

<template>
  <div class="metric-tile" :class="tone ? `metric-tile--${tone}` : undefined">
    <span class="metric-tile__label">{{ label }}</span>
    <strong class="metric-tile__value">{{ value }}</strong>
    <span v-if="code" class="metric-tile__code">{{ code }}</span>
    <span v-if="detail" class="metric-tile__detail">{{ detail }}</span>
  </div>
</template>
