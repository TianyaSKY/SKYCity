<script setup lang="ts">
import { computed } from 'vue';
import { useWorldStore } from '../stores/worldStore';

const store = useWorldStore();

const pad2 = (n: number): string => String(n).padStart(2, '0');

const openHours = computed(() => {
  const loc = store.selectedLocation;
  return loc ? `${pad2(loc.open_hour)}:00 – ${pad2(loc.close_hour)}:00` : '';
});

const worldClock = computed(() => {
  const hours = Math.floor(store.worldTimeMinutes / 60) % 24;
  const minutes = store.worldTimeMinutes % 60;
  return `${pad2(hours)}:${pad2(minutes)}`;
});
</script>

<template>
  <div class="location-panel">
    <div class="clock">世界时间 {{ worldClock }}</div>
    <template v-if="store.selectedLocation">
      <h3>{{ store.selectedLocation.name }}</h3>
      <dl>
        <div class="row"><dt>类型</dt><dd>{{ store.selectedLocation.location_type }}</dd></div>
        <div class="row"><dt>容量</dt><dd>{{ store.selectedLocation.capacity }}</dd></div>
        <div class="row"><dt>营业</dt><dd>{{ openHours }}</dd></div>
        <div class="row"><dt>坐标</dt><dd>({{ store.selectedLocation.col }}, {{ store.selectedLocation.row }})</dd></div>
      </dl>
    </template>
    <p v-else class="hint">点击地图上的地点查看详情</p>
  </div>
</template>

<style scoped>
.location-panel {
  min-width: 190px;
  padding: 10px 14px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.6);
  border: 1px solid rgba(255, 255, 255, 0.08);
  color: #e8f5e9;
  font-size: 12px;
}
.clock {
  margin-bottom: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  opacity: 0.7;
}
h3 {
  margin-bottom: 6px;
  font-size: 14px;
  color: #ffe082;
}
.row {
  display: flex;
  gap: 8px;
  margin: 3px 0;
}
dt {
  width: 32px;
  opacity: 0.65;
}
dd {
  margin: 0;
}
.hint {
  opacity: 0.6;
}
</style>
