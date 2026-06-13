<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  rows: Array<{ label: string; value: number; code?: string }>;
}>();

const maxValue = computed(() => Math.max(1, ...props.rows.map((row) => row.value)));
</script>

<template>
  <div class="mini-bars">
    <div v-for="row in rows" :key="row.label" class="mini-bars__row">
      <span>{{ row.label }}</span>
      <div class="mini-bars__track">
        <i :style="{ width: `${Math.max(4, (row.value / maxValue) * 100)}%` }" />
      </div>
      <strong>{{ row.value }}</strong>
      <code v-if="row.code">{{ row.code }}</code>
    </div>
  </div>
</template>
