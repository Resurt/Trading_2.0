<script setup lang="ts">
defineProps<{
  title: string;
  detail?: string;
  tone?: "neutral" | "warn" | "bad";
}>();
</script>

<template>
  <div class="empty-state" :class="tone ? `empty-state--${tone}` : undefined">
    <strong>{{ title }}</strong>
    <p v-if="detail">{{ detail }}</p>
  </div>
</template>
