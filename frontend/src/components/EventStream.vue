<script lang="ts" setup>
import {computed} from 'vue';
import {useWorldStore} from '../stores/worldStore';

const store = useWorldStore();

const visible = computed(() => store.events.slice(0, 6));

function timeOf(worldTime: number): string {
    const hours = Math.floor(worldTime / 60) % 24;
    const minutes = worldTime % 60;
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
}
</script>

<template>
    <div class="event-stream">
        <div v-for="ev in visible" :key="ev.sequence" class="event-line">
            <span class="ev-time">{{ timeOf(ev.worldTime) }}</span>
            <span class="ev-text">{{ ev.text }}</span>
        </div>
    </div>
</template>

<style scoped>
.event-stream {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 6px 14px;
    border-radius: 10px;
    background: rgba(0, 0, 0, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.08);
    color: #cde8d5;
    font-size: 12px;
    min-width: 320px;
    max-width: 640px;
}

.event-line {
    display: flex;
    gap: 8px;
    align-items: baseline;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.ev-time {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    color: #9fd8ae;
    opacity: 0.8;
    flex-shrink: 0;
}

.ev-text {
    overflow: hidden;
    text-overflow: ellipsis;
}

.event-line:first-child .ev-text {
    color: #ffe082;
}
</style>
