<script setup lang="ts">
import { computed } from "vue";

import { codeWithLabel, severityForCode } from "../../utils/labels";

const props = defineProps<{
  code: string | null | undefined;
  label?: string;
  compact?: boolean;
}>();

const tone = computed(() => severityForCode(props.code));
</script>

<template>
  <span class="status-pill" :class="`status-pill--${tone}`">
    <span class="status-pill__dot" />
    <span>{{ label ?? codeWithLabel(code) }}</span>
    <code v-if="!compact && label && code">{{ code }}</code>
  </span>
</template>
