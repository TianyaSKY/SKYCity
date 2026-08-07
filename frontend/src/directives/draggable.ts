/**
 * v-draggable: make a HUD panel freely draggable.
 *
 * Usage: `<div v-draggable="'unique-key'">…</div>`
 *
 * - Drag with the left button on any non-interactive area of the panel
 *   (buttons, inputs, selects, links, and scroll frames pass through).
 * - The position is persisted per key in localStorage and restored on mount.
 * - The drag clamps the panel so at least a corner stays on screen.
 */
import type {Directive} from 'vue';

const STORAGE_PREFIX = 'ai-town.panel.';
const DRAG_THRESHOLD = 4; // px of movement before a drag counts
const MIN_VISIBLE = 60; // px of the panel kept on screen while dragging

/** Elements that should never start a drag; their clicks pass through. */
const INTERACTIVE_SELECTOR =
    'button, input, select, textarea, a, label, [contenteditable="true"], [role="button"], [role="tab"], [role="menuitem"]';

interface SavedPos {
    left: number;
    top: number;
}

interface DragSession {
    pointerId: number;
    startX: number;
    startY: number;
    originLeft: number;
    originTop: number;
    moved: boolean;
}

interface DragHandlers {
    onPointerDown: (e: PointerEvent) => void;
    onPointerMove: (e: PointerEvent) => void;
    onPointerUp: (e: PointerEvent) => void;
}

const sessions = new WeakMap<HTMLElement, DragHandlers>();

function loadPos(key: string): SavedPos | null {
    try {
        const raw = localStorage.getItem(STORAGE_PREFIX + key);
        return raw ? (JSON.parse(raw) as SavedPos) : null;
    } catch {
        return null;
    }
}

function savePos(key: string, pos: SavedPos): void {
    try {
        localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(pos));
    } catch {
        // Storage can be unavailable (private mode); the drag still works in-session.
    }
}

/** Switch the element to fixed positioning at the given viewport point. */
function applyPos(el: HTMLElement, left: number, top: number): void {
    el.style.position = 'fixed';
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    el.style.right = 'auto';
    el.style.bottom = 'auto';
    el.style.transform = 'none';
    el.style.margin = '0';
}

/** A scrollable element hit on its own frame is a scrollbar drag, not a panel drag. */
function isScrollFrame(target: HTMLElement): boolean {
    return target.scrollHeight > target.clientHeight || target.scrollWidth > target.clientWidth;
}

function shouldStartDrag(el: HTMLElement, target: EventTarget | null): boolean {
    if (!(target instanceof HTMLElement)) return false;
    // Clicking an inner scroll container's frame (the scrollbar) keeps scrolling.
    if (target !== el && isScrollFrame(target)) return false;
    return target.closest(INTERACTIVE_SELECTOR) === null;
}

export const vDraggable: Directive<HTMLElement, string> = {
    mounted(el, binding) {
        const key = binding.value || 'default';
        const saved = loadPos(key);
        if (saved) applyPos(el, saved.left, saved.top);

        let session: DragSession | null = null;

        const onPointerDown = (e: PointerEvent): void => {
            if (e.button !== 0 || session) return;
            if (!shouldStartDrag(el, e.target)) return;
            const rect = el.getBoundingClientRect();
            session = {
                pointerId: e.pointerId,
                startX: e.clientX,
                startY: e.clientY,
                originLeft: rect.left,
                originTop: rect.top,
                moved: false,
            };
            // Switch to fixed at the current spot so the drag never jumps.
            applyPos(el, rect.left, rect.top);
            el.style.zIndex = '999';
            el.style.userSelect = 'none';
            window.addEventListener('pointermove', onPointerMove);
            window.addEventListener('pointerup', onPointerUp);
        };

        const onPointerMove = (e: PointerEvent): void => {
            if (!session || e.pointerId !== session.pointerId) return;
            const dx = e.clientX - session.startX;
            const dy = e.clientY - session.startY;
            if (!session.moved && Math.abs(dx) < DRAG_THRESHOLD && Math.abs(dy) < DRAG_THRESHOLD) return;
            session.moved = true;
            const vw = window.innerWidth;
            const vh = window.innerHeight;
            const w = el.offsetWidth;
            applyPos(
                el,
                Math.min(Math.max(session.originLeft + dx, -w + MIN_VISIBLE), vw - MIN_VISIBLE),
                Math.min(Math.max(session.originTop + dy, 0), vh - 40),
            );
        };

        const onPointerUp = (e: PointerEvent): void => {
            if (!session || e.pointerId !== session.pointerId) return;
            if (session.moved) {
                savePos(key, {left: parseFloat(el.style.left), top: parseFloat(el.style.top)});
            }
            el.style.zIndex = '';
            el.style.userSelect = '';
            session = null;
            window.removeEventListener('pointermove', onPointerMove);
            window.removeEventListener('pointerup', onPointerUp);
        };

        el.addEventListener('pointerdown', onPointerDown);
        sessions.set(el, {onPointerDown, onPointerMove, onPointerUp});
    },
    unmounted(el) {
        const handlers = sessions.get(el);
        if (!handlers) return;
        el.removeEventListener('pointerdown', handlers.onPointerDown);
        window.removeEventListener('pointermove', handlers.onPointerMove);
        window.removeEventListener('pointerup', handlers.onPointerUp);
        sessions.delete(el);
    },
};
