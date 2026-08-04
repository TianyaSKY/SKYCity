/**
 * Pointer-drag panning + integer-level wheel zoom for the world container.
 * Zoom is clamped to [1, 2, 3, 4, 8] and anchored at the pointer position.
 */

import type { Application, Container } from 'pixi.js';

export const ZOOM_LEVELS = [1, 2, 3, 4, 8] as const;

export interface CameraEvents {
  onZoomChanged?: (zoom: number) => void;
  onPanChanged?: (x: number, y: number) => void;
}

const TAP_THRESHOLD_PX = 5;

export class CameraController {
  private readonly canvas: HTMLCanvasElement;
  private zoomIndex = 0;
  private dragging = false;
  private lastX = 0;
  private lastY = 0;
  private moved = 0;
  private tapped = false;

  constructor(
    app: Application,
    private readonly world: Container,
    private readonly events: CameraEvents = {},
  ) {
    this.canvas = app.canvas;
  }

  get zoom(): number {
    return ZOOM_LEVELS[this.zoomIndex];
  }

  attach(): void {
    const canvas = this.canvas;
    canvas.addEventListener('pointerdown', this.handlePointerDown);
    canvas.addEventListener('pointermove', this.handlePointerMove);
    canvas.addEventListener('pointerup', this.handlePointerUp);
    canvas.addEventListener('wheel', this.handleWheel, { passive: false });
  }

  detach(): void {
    const canvas = this.canvas;
    canvas.removeEventListener('pointerdown', this.handlePointerDown);
    canvas.removeEventListener('pointermove', this.handlePointerMove);
    canvas.removeEventListener('pointerup', this.handlePointerUp);
    canvas.removeEventListener('wheel', this.handleWheel);
  }

  /** True when the most recent gesture was a tap (drag below threshold). */
  wasTap(): boolean {
    return this.tapped;
  }

  screenToWorld(x: number, y: number): { x: number; y: number } {
    return {
      x: (x - this.world.position.x) / this.world.scale.x,
      y: (y - this.world.position.y) / this.world.scale.y,
    };
  }

  worldToScreen(x: number, y: number): { x: number; y: number } {
    return {
      x: x * this.world.scale.x + this.world.position.x,
      y: y * this.world.scale.y + this.world.position.y,
    };
  }

  private readonly handlePointerDown = (e: PointerEvent): void => {
    this.dragging = true;
    this.moved = 0;
    this.tapped = false;
    this.lastX = e.clientX;
    this.lastY = e.clientY;
    this.canvas.setPointerCapture(e.pointerId);
  };

  private readonly handlePointerMove = (e: PointerEvent): void => {
    if (!this.dragging) return;
    const dx = e.clientX - this.lastX;
    const dy = e.clientY - this.lastY;
    this.lastX = e.clientX;
    this.lastY = e.clientY;
    this.moved += Math.abs(dx) + Math.abs(dy);
    this.world.position.x += dx;
    this.world.position.y += dy;
    this.events.onPanChanged?.(this.world.position.x, this.world.position.y);
  };

  private readonly handlePointerUp = (): void => {
    this.dragging = false;
    // Pointer capture is released automatically on pointerup.
    this.tapped = this.moved < TAP_THRESHOLD_PX;
  };

  private readonly handleWheel = (e: WheelEvent): void => {
    e.preventDefault();
    const next = e.deltaY < 0 ? this.zoomIndex + 1 : this.zoomIndex - 1;
    if (next < 0 || next >= ZOOM_LEVELS.length || next === this.zoomIndex) return;

    const rect = this.canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const oldZoom = this.zoom;
    const newZoom = ZOOM_LEVELS[next];

    // Keep the world point under the pointer stationary.
    const wx = (px - this.world.position.x) / oldZoom;
    const wy = (py - this.world.position.y) / oldZoom;
    this.zoomIndex = next;
    this.world.scale.set(newZoom);
    this.world.position.set(px - wx * newZoom, py - wy * newZoom);

    this.events.onZoomChanged?.(newZoom);
    this.events.onPanChanged?.(this.world.position.x, this.world.position.y);
  };
}
