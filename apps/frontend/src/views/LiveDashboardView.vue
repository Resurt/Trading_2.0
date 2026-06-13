<template>
  <section class="page-stack">
    <div class="page-heading">
      <h1>Live Dashboard</h1>
      <p>Текущий рынок, состояние робота, сессия и последние risk events.</p>
    </div>

    <div class="dashboard-grid">
      <article class="panel">
        <h2>Session</h2>
        <dl>
          <dt>session_type</dt>
          <dd>weekday_morning</dd>
          <dt>session_phase</dt>
          <dd>closed</dd>
          <dt>micro_session_id</dt>
          <dd>not_started</dd>
        </dl>
      </article>

      <article class="panel">
        <h2>Market</h2>
        <dl>
          <dt>spread</dt>
          <dd>placeholder</dd>
          <dt>mid price</dt>
          <dd>placeholder</dd>
          <dt>market quality</dt>
          <dd>placeholder</dd>
        </dl>
      </article>

      <article class="panel">
        <h2>Robot</h2>
        <dl>
          <dt>strategy_state</dt>
          <dd>skeleton</dd>
          <dt>current blocker</dt>
          <dd>none</dd>
          <dt>active orders</dt>
          <dd>0</dd>
        </dl>
      </article>
    </div>
  </section>
</template>
