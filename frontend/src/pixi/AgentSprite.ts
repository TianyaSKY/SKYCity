/**
 * One agent's visual: a shadow ellipse, a rounded-rect body and a head,
 * all in the agent's assigned color. The container's origin sits at the
 * agent's feet (col*TILE+8, row*TILE+12 in world pixels). Visual state is
 * conveyed by body lightness plus a tiny "…" dot above the head while
 * waiting.
 */

import {Container, Graphics} from 'pixi.js';

export type AgentVisual = 'idle' | 'moving' | 'waiting';

export const AGENT_TILE_SIZE = 16;

/** Parse a "#rrggbb" color string into a 0xrrggbb number. */
function hexColor(css: string): number {
    const hex = css.replace('#', '');
    return Number.parseInt(hex, 16);
}

/** Linear mix of two 0xrrggbb colors; t=0 -> a, t=1 -> b. */
function mixHex(a: number, b: number, t: number): number {
    const ar = (a >> 16) & 0xff;
    const ag = (a >> 8) & 0xff;
    const ab = a & 0xff;
    const br = (b >> 16) & 0xff;
    const bg = (b >> 8) & 0xff;
    const bb = b & 0xff;
    return (
        ((Math.round(ar + (br - ar) * t) << 16) |
            (Math.round(ag + (bg - ag) * t) << 8) |
            Math.round(ab + (bb - ab) * t)) >>>
        0
    );
}

export class AgentSprite extends Container {
    private readonly shadow: Graphics;
    private readonly talkRing: Graphics;
    private readonly body: Graphics;
    private readonly head: Graphics;
    private readonly waitDot: Graphics;
    private readonly selectionMark: Graphics;
    private readonly baseColor: number;
    private readonly movingColor: number;
    private readonly waitingColor: number;
    private visual: AgentVisual = 'idle';

    constructor(color: string) {
        super();
        this.baseColor = hexColor(color);
        this.movingColor = mixHex(this.baseColor, 0xffffff, 0.35);
        this.waitingColor = mixHex(this.baseColor, 0x000000, 0.25);

        this.shadow = new Graphics();
        this.talkRing = new Graphics();
        this.body = new Graphics();
        this.head = new Graphics();
        this.waitDot = new Graphics();
        this.selectionMark = new Graphics();

        this.shadow.ellipse(0, 0, 8, 4).fill({color: 0x000000, alpha: 0.25});
        this.talkRing.circle(0, -16, 9).stroke({color: 0xffd54a, width: 1.5, alpha: 0.9});
        this.talkRing.visible = false;
        this.selectionMark
            .moveTo(0, -30)
            .lineTo(-4, -24.5)
            .lineTo(4, -24.5)
            .closePath()
            .fill({color: 0xffffff, alpha: 0.95});
        this.selectionMark.visible = false;
        this.waitDot.circle(0, -21, 1.6).fill({color: 0xffffff, alpha: 0.9});
        this.waitDot.visible = false;
        this.redrawBody();
        this.addChild(this.shadow, this.talkRing, this.body, this.head, this.waitDot, this.selectionMark);
    }

    /** Snap to a cell; feet anchor at col*TILE+8, row*TILE+12. */
    setCell(col: number, row: number): void {
        this.position.set(col * AGENT_TILE_SIZE + 8, row * AGENT_TILE_SIZE + 12);
    }

    /** Position from a fractional (interpolated) col/row. */
    setCellFrac(col: number, row: number): void {
        this.setCell(col, row);
    }

    setVisual(visual: AgentVisual): void {
        if (visual === this.visual) return;
        this.visual = visual;
        this.waitDot.visible = visual === 'waiting';
        this.redrawBody();
    }

    /** Show/hide the amber halo while the agent is in an active conversation. */
    setTalkPartner(active: boolean): void {
        this.talkRing.visible = active;
    }

    /** Show/hide the white marker above the head when the agent is selected. */
    setSelected(selected: boolean): void {
        this.selectionMark.visible = selected;
    }

    private redrawBody(): void {
        const fill = this.visual === 'moving' ? this.movingColor : this.visual === 'waiting' ? this.waitingColor : this.baseColor;
        this.body.clear();
        this.body.roundRect(-6, -13, 12, 11, 3).fill({color: fill});
        this.head.clear();
        this.head.circle(0, -16, 3.5).fill({color: fill});
    }
}
