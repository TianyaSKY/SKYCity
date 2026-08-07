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
  const hours = Math.floor(store.worldTime / 60) % 24;
  const minutes = store.worldTime % 60;
  return `${pad2(hours)}:${pad2(minutes)}`;
});

/** Chinese label for a location_type; falls back to the raw id. */
function typeLabel(locationType: string): string {
  const labels: Record<string, string> = {
    store: '商店',
    farm: '农场',
    plaza: '广场',
    office: '政务厅',
    house: '住宅',
    hotel: '旅店',
  };
  return labels[locationType] ?? locationType;
}

/** Whether the selected location is open right now (snapshot flag or hours). */
const isOpenNow = computed(() => {
  const loc = store.selectedLocation;
  if (!loc) return false;
  if (store.locationDetail) return store.locationDetail.open;
  return store.isOpen(loc.location_id);
});

/** In-shop capacity display: "2 / 8" when detail has arrived, else capacity. */
const occupancyText = computed(() => {
  const detail = store.locationDetail;
  if (!detail) return '';
  return `${detail.occupants.length} / ${detail.capacity}`;
});
</script>

<template>
  <div class="location-panel">
    <div class="clock">世界时间 {{ worldClock }}</div>
    <template v-if="store.selectedLocation">
      <h3>{{ store.selectedLocation.name }}</h3>
      <dl>
        <div class="row"><dt>类型</dt><dd>{{ typeLabel(store.selectedLocation.location_type) }}</dd></div>
        <div class="row"><dt>容量</dt><dd>{{ store.selectedLocation.capacity }}</dd></div>
        <div class="row"><dt>营业</dt><dd>{{ openHours }}</dd></div>
        <div class="row"><dt>状态</dt><dd>
          <span class="open-state" :class="isOpenNow ? 'open' : 'closed'">
            {{ isOpenNow ? '营业中' : '已关门' }}
          </span>
        </dd></div>
        <div class="row"><dt>坐标</dt><dd>({{ store.selectedLocation.col }}, {{ store.selectedLocation.row }})</dd></div>
      </dl>

      <template v-if="store.locationDetail">
        <div class="section-title">在店 <span class="sec-count">{{ occupancyText }}</span></div>
        <div v-if="store.locationDetail.occupants.length" class="occupants">
          <span v-for="o in store.locationDetail.occupants" :key="o.agent_id" class="occupant-chip">
            {{ o.name }}
          </span>
        </div>
        <p v-else class="sec-empty">无人</p>

        <template v-if="store.locationDetail.products.length">
          <div class="section-title">商品</div>
          <ul class="sec-list">
            <li v-for="p in store.locationDetail.products" :key="p.item_id" class="product-row">
              <span class="p-name">{{ p.name }}</span>
              <span class="p-price">售{{ p.sell_price }} / 收{{ p.buy_price }}</span>
              <span class="p-stock" :class="{ low: p.stock <= 0 }">×{{ p.stock }}</span>
            </li>
          </ul>
        </template>

        <template v-if="store.locationDetail.jobs.length">
          <div class="section-title">可做的工作</div>
          <ul class="sec-list">
            <li v-for="j in store.locationDetail.jobs" :key="j.job_id" class="job-row">
              <span class="j-name">{{ j.name }}</span>
              <span class="j-pay">{{ j.wage }} 金/次 · {{ j.duration_minutes }} 分钟</span>
            </li>
          </ul>
        </template>
      </template>
      <p v-else class="sec-loading">详情加载中…</p>
    </template>
    <p v-else class="hint">点击地图上的地点查看详情</p>
  </div>
</template>

<style scoped>
.location-panel {
  min-width: 230px;
  max-width: 260px;
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
  flex-shrink: 0;
}
dd {
  margin: 0;
}
.open-state.open {
  color: #81c784;
}
.open-state.closed {
  color: #e57373;
}
.section-title {
  margin: 10px 0 5px;
  padding-top: 8px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  font-weight: 600;
  color: #ffe082;
}
.sec-count {
  font-weight: 400;
  opacity: 0.65;
}
.occupants {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.occupant-chip {
  padding: 1px 8px;
  border-radius: 999px;
  background: rgba(129, 199, 132, 0.18);
  border: 1px solid rgba(129, 199, 132, 0.4);
  color: #c8e6c9;
}
.sec-empty,
.sec-loading {
  margin: 2px 0;
  opacity: 0.55;
}
.sec-list {
  margin: 2px 0 0;
  padding: 0;
  list-style: none;
}
.product-row,
.job-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin: 3px 0;
}
.p-name,
.j-name {
  flex: 1;
  color: #ffe0b2;
}
.p-price,
.p-stock {
  opacity: 0.7;
  white-space: nowrap;
}
.p-stock.low {
  color: #e57373;
  opacity: 1;
}
.j-pay {
  opacity: 0.7;
  white-space: nowrap;
}
.hint {
  opacity: 0.6;
}
</style>
