/**
 * v-draggable directive: drag moves the panel, clicks on interactive
 * children pass through, and the position persists across mounts.
 */
import {beforeEach, describe, expect, it} from 'vitest';
import {mount} from '@vue/test-utils';
import {vDraggable} from './draggable';

const DraggablePanel = {
    template: '<div v-draggable="\'p\'">content</div>',
    directives: {draggable: vDraggable},
};

const InteractivePanel = {
    template: '<div v-draggable="\'p\'"><button class="btn">go</button></div>',
    directives: {draggable: vDraggable},
};

function mockRect(el: HTMLElement): void {
    const rect = {left: 120, top: 80, right: 320, bottom: 180, width: 200, height: 100, x: 120, y: 80};
    el.getBoundingClientRect = () => rect as DOMRect;
    Object.defineProperty(el, 'offsetWidth', {value: 200});
    Object.defineProperty(el, 'offsetHeight', {value: 100});
}

function drag(el: HTMLElement, dx: number, dy: number): void {
    el.dispatchEvent(
        new PointerEvent('pointerdown', {bubbles: true, button: 0, pointerId: 1, clientX: 150, clientY: 90}),
    );
    window.dispatchEvent(
        new PointerEvent('pointermove', {bubbles: true, pointerId: 1, clientX: 150 + dx, clientY: 90 + dy}),
    );
    window.dispatchEvent(
        new PointerEvent('pointerup', {bubbles: true, pointerId: 1, clientX: 150 + dx, clientY: 90 + dy}),
    );
}

beforeEach(() => {
    localStorage.clear();
});

describe('v-draggable', () => {
    it('moves the panel to the dragged position and persists it', () => {
        const wrapper = mount(DraggablePanel);
        mockRect(wrapper.element);
        drag(wrapper.element, 10, 20);
        expect(wrapper.element.style.position).toBe('fixed');
        expect(wrapper.element.style.left).toBe('130px');
        expect(wrapper.element.style.top).toBe('100px');
        expect(localStorage.getItem('ai-town.panel.p')).toBe('{"left":130,"top":100}');
    });

    it('does not start a drag on an interactive child', () => {
        const wrapper = mount(InteractivePanel);
        mockRect(wrapper.element);
        drag(wrapper.find('button').element, 10, 20);
        expect(wrapper.element.style.position).not.toBe('fixed');
        expect(localStorage.getItem('ai-town.panel.p')).toBeNull();
    });

    it('ignores tiny jitters: no drag, no persistence', () => {
        const wrapper = mount(DraggablePanel);
        mockRect(wrapper.element);
        drag(wrapper.element, 2, 3); // below DRAG_THRESHOLD
        expect(wrapper.element.style.left).not.toBe('122px');
        expect(localStorage.getItem('ai-town.panel.p')).toBeNull();
    });

    it('restores a saved position on mount', () => {
        localStorage.setItem('ai-town.panel.p', JSON.stringify({left: 42, top: 24}));
        const wrapper = mount(DraggablePanel);
        expect(wrapper.element.style.position).toBe('fixed');
        expect(wrapper.element.style.left).toBe('42px');
        expect(wrapper.element.style.top).toBe('24px');
    });

    it('clamps the panel to stay on screen', () => {
        const wrapper = mount(DraggablePanel);
        mockRect(wrapper.element);
        drag(wrapper.element, -500, -500);
        const left = parseFloat(wrapper.element.style.left);
        const top = parseFloat(wrapper.element.style.top);
        expect(left).toBeGreaterThanOrEqual(-200 + 60); // -w + MIN_VISIBLE
        expect(top).toBeGreaterThanOrEqual(0);
        expect(top).toBeLessThanOrEqual(window.innerHeight - 40);
    });
});
