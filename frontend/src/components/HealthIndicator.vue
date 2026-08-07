<script lang="ts" setup>
import {computed} from 'vue';
import {useWorldStore} from '../stores/worldStore';

const store = useWorldStore();

const label = computed(() => {
    if (store.healthOk) return 'API 正常';
    return store.health ? `API ${store.health.status}` : 'API 离线';
});

const version = computed(() => store.health?.map_version ?? '—');
</script>

<template>
    <div :class="store.healthOk ? 'ok' : 'bad'" class="health-pill">
        <span class="dot"/>
        <span>{{ label }}</span>
        <span class="version">map v{{ version }}</span>
    </div>
</template>

<style scoped>
.health-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    padding: 5px 12px;
    border-radius: 999px;
    background: rgba(0, 0, 0, 0.55);
    color: #e8f5e9;
    border: 1px solid transparent;
}

.health-pill.ok {
    border-color: rgba(76, 175, 80, 0.6);
}

.health-pill.bad {
    border-color: rgba(229, 57, 53, 0.7);
}

.dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
}

.ok .dot {
    background: #4caf50;
    box-shadow: 0 0 6px #4caf50;
}

.bad .dot {
    background: #e53935;
    box-shadow: 0 0 6px #e53935;
}

.version {
    opacity: 0.75;
}
</style>
