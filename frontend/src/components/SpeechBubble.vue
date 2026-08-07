<script lang="ts" setup>
import {onBeforeUnmount, onMounted} from 'vue';
import type {BubbleItem} from '../stores/worldStore';

/**
 * DOM overlay rendering live speech bubbles above agents. Bubbles come from
 * the store (one per agent, auto-pruned after BUBBLE_TTL_MS); this component
 * only tracks each bubble element and repositions it on every animation
 * frame to follow the agent's interpolated position + camera pan/zoom.
 */

const props = defineProps<{
    bubbles: BubbleItem[];
    agentScreenPos: (agentId: string) => { x: number; y: number } | null;
}>();

const els = new Map<string, HTMLElement>();

function setEl(id: string, el: unknown): void {
    if (el instanceof HTMLElement) els.set(id, el);
    else els.delete(id);
}

let raf = 0;

function frame(): void {
    for (const bubble of props.bubbles) {
        const el = els.get(bubble.id);
        if (!el) continue;
        const pos = props.agentScreenPos(bubble.agent_id);
        if (!pos) {
            el.style.visibility = 'hidden';
            continue;
        }
        el.style.visibility = 'visible';
        // Anchor the chip's bottom-center just above the agent's head.
        el.style.transform = `translate(${pos.x}px, ${pos.y}px) translate(-50%, -100%) translateY(-26px)`;
    }
    raf = requestAnimationFrame(frame);
}

onMounted(() => {
    raf = requestAnimationFrame(frame);
});

onBeforeUnmount(() => {
    cancelAnimationFrame(raf);
    els.clear();
});
</script>

<template>
    <div aria-hidden="true" class="bubble-overlay">
        <div v-for="b in bubbles" :key="b.id" :ref="(el) => setEl(b.id, el)" class="bubble">
            <span class="bubble-text">{{ b.text }}</span>
            <span class="bubble-arrow"/>
        </div>
    </div>
</template>

<style scoped>
.bubble-overlay {
    position: absolute;
    inset: 0;
    pointer-events: none;
}

.bubble {
    position: absolute;
    left: 0;
    top: 0;
    max-width: 220px;
    padding: 6px 11px;
    border-radius: 10px;
    background: rgba(16, 20, 17, 0.92);
    border: 1px solid rgba(255, 255, 255, 0.14);
    color: #ffffff;
    font-size: 12px;
    line-height: 1.35;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.4);
    will-change: transform;
}

.bubble-arrow {
    position: absolute;
    left: 50%;
    bottom: -5px;
    width: 0;
    height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid rgba(16, 20, 17, 0.92);
    transform: translateX(-50%);
}
</style>
