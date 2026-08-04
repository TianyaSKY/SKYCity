<script setup lang="ts">
import { useWorldStore } from '../stores/worldStore';

const store = useWorldStore();

const SPEEDS = [1, 2, 5, 10] as const;

const connLabel = (() => ({ connected: '已连接', connecting: '连接中', disconnected: '未连接' }))();
const worldShort = (): string => store.worldId?.replace(/^world_/, '') ?? '—';
</script>

<template>
  <div class="clock-bar">
    <span class="conn" :class="store.connection"><span class="dot" />{{ connLabel[store.connection] }}</span>
    <span class="chip">世界 #{{ worldShort() }}</span>
    <span class="time">{{ store.timeLabel }}</span>
    <span class="day">{{ store.dayLabel }}</span>
    <span class="weather">{{ store.weatherLabel }}</span>
    <span class="spacer" />
    <div class="speed-group">
      <button
        v-for="s in SPEEDS"
        :key="s"
        class="speed-btn"
        :class="{ active: store.speed === s }"
        :disabled="store.connection !== 'connected'"
        @click="store.setSpeed(s)"
      >
        {{ s }}×
      </button>
    </div>
    <button
      class="pause-btn"
      :class="{ paused: store.paused }"
      :disabled="store.connection !== 'connected'"
      @click="store.togglePause()"
    >
      {{ store.paused ? '继续' : '暂停' }}
    </button>
  </div>
</template>

<style scoped>
.clock-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 5px 14px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.6);
  border: 1px solid rgba(255, 255, 255, 0.08);
  color: #cde8d5;
  font-size: 12px;
  white-space: nowrap;
}
.conn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.conn .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.conn.connected .dot {
  background: #4caf50;
  box-shadow: 0 0 6px #4caf50;
}
.conn.connecting .dot {
  background: #ffb300;
  box-shadow: 0 0 6px #ffb300;
}
.conn.disconnected .dot {
  background: #e53935;
  box-shadow: 0 0 6px #e53935;
}
.chip {
  padding: 1px 8px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.1);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.time {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 15px;
  font-weight: 600;
  color: #ffe082;
}
.day,
.weather {
  opacity: 0.85;
}
.spacer {
  flex: 1;
  min-width: 8px;
}
.speed-group {
  display: inline-flex;
  gap: 4px;
}
.speed-btn,
.pause-btn {
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: rgba(255, 255, 255, 0.06);
  color: #cde8d5;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.speed-btn.active {
  background: rgba(76, 175, 80, 0.35);
  border-color: rgba(76, 175, 80, 0.8);
  color: #e8f5e9;
}
.speed-btn:disabled,
.pause-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
.pause-btn.paused {
  background: rgba(255, 179, 0, 0.25);
  border-color: rgba(255, 179, 0, 0.7);
}
</style>
