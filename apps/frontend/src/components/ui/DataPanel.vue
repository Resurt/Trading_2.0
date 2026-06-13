<template>
  <article class="panel data-panel">
    <header class="panel-header">
      <div>
        <p v-if="$slots.eyebrow" class="eyebrow"><slot name="eyebrow" /></p>
        <h2><slot name="title" /></h2>
      </div>
      <div v-if="$slots.action" class="panel-action">
        <slot name="action" />
      </div>
    </header>
    <slot />
  </article>
</template>
