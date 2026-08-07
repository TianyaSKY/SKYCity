<script lang="ts" setup>
import {computed, ref} from 'vue';
import {useWorldStore} from '../stores/worldStore';

/**
 * 小镇股市: read-only quote board + shareholder list (M10). All data comes
 * from the store — REST-loaded on snapshot, kept live by the WS events
 * (stock_price_changed / stock_bought / stock_sold / dividend_paid).
 */

const store = useWorldStore();
const collapsed = ref(false);

/** One quote row: name, live price, delta vs 昨收, today's business, last dividend. */
const rows = computed(() =>
    store.stocks.map((s) => {
        const delta = s.price - s.prev_price;
        return {
            stock_id: s.stock_id,
            name: s.name,
            price: s.price,
            delta,
            day_business: s.day_business,
            last_div: s.last_div_per_share,
        };
    }),
);

/** Flatten store.holdings (agent_id → stock_id → shares) into display rows. */
const holdingsRows = computed(() => {
    const stockName = (stockId: string): string =>
        store.stocks.find((s) => s.stock_id === stockId)?.name ?? stockId;
    const rows: { agentName: string; stockName: string; shares: number; value: number }[] = [];
    for (const [agentId, byStock] of Object.entries(store.holdings)) {
        for (const [stockId, shares] of Object.entries(byStock)) {
            if (shares <= 0) continue;
            const price = store.stocks.find((s) => s.stock_id === stockId)?.price ?? 0;
            rows.push({
                agentName: store.agentById(agentId)?.name ?? agentId,
                stockName: stockName(stockId),
                shares,
                value: shares * price,
            });
        }
    }
    rows.sort((a, b) => b.value - a.value);
    return rows;
});
</script>

<template>
    <aside aria-label="小镇股市" class="stock-panel">
        <header class="sp-head" @click="collapsed = !collapsed">
            <span class="sp-title">小镇股市</span>
            <button :title="collapsed ? '展开' : '折叠'" class="sp-toggle">{{ collapsed ? '▸' : '▾' }}</button>
        </header>
        <template v-if="!collapsed">
            <p v-if="rows.length === 0" class="sp-empty">暂无股票行情</p>
            <table v-else class="sp-table">
                <thead>
                <tr>
                    <th>名称</th>
                    <th class="num">现价</th>
                    <th class="num">涨跌</th>
                    <th class="num">今日业绩</th>
                    <th class="num">每股分红</th>
                </tr>
                </thead>
                <tbody>
                <tr v-for="row in rows" :key="row.stock_id">
                    <td class="sp-name">{{ row.name }}</td>
                    <td class="num">{{ row.price }}</td>
                    <td :style="{ color: row.delta > 0 ? '#81c784' : row.delta < 0 ? '#ff8a65' : 'inherit' }"
                        class="num">
                        {{ row.delta > 0 ? `+${row.delta}` : row.delta }}
                    </td>
                    <td class="num">{{ row.day_business }}</td>
                    <td class="num">{{ row.last_div }}</td>
                </tr>
                </tbody>
            </table>
            <div class="sp-section">股东持仓</div>
            <p v-if="holdingsRows.length === 0" class="sp-empty">暂无股东</p>
            <ul v-else class="sp-holdings">
                <li v-for="(h, i) in holdingsRows" :key="i" class="sp-holding">
                    <span class="sp-holder">{{ h.agentName }}</span>
                    <span class="sp-held">{{ h.stockName }} {{ h.shares }}股</span>
                    <span class="sp-value">{{ h.value }} 金币</span>
                </li>
            </ul>
        </template>
    </aside>
</template>

<style scoped>
.stock-panel {
    width: 260px;
    max-height: min(52vh, 520px);
    display: flex;
    flex-direction: column;
    border-radius: 10px;
    background: rgba(6, 12, 9, 0.8);
    border: 1px solid rgba(255, 255, 255, 0.1);
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4);
    color: #cde8d5;
    font-size: 11.5px;
    pointer-events: auto;
    backdrop-filter: blur(4px);
    overflow: hidden;
}

.sp-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 7px 12px 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    user-select: none;
    cursor: pointer;
}

.sp-title {
    flex: 1;
    font-weight: 600;
    color: #ffe082;
    font-size: 12px;
}

.sp-toggle {
    background: none;
    border: none;
    color: rgba(205, 232, 213, 0.55);
    font-size: 12px;
    cursor: pointer;
    padding: 0;
}

.sp-empty {
    margin: 0;
    padding: 12px 8px;
    text-align: center;
    color: rgba(205, 232, 213, 0.5);
}

.sp-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
}

.sp-table th {
    text-align: left;
    font-weight: 500;
    color: rgba(205, 232, 213, 0.5);
    padding: 4px 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.sp-table th.num {
    text-align: right;
}

.sp-table td {
    padding: 3px 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}

.sp-table td.num {
    text-align: right;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.sp-name {
    font-weight: 600;
    color: #e8f5e9;
}

.sp-section {
    padding: 6px 12px 4px;
    color: rgba(205, 232, 213, 0.5);
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    font-size: 11px;
}

.sp-holdings {
    list-style: none;
    margin: 0;
    padding: 4px 6px 8px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 1px;
}

.sp-holding {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 6px;
    border-radius: 6px;
    line-height: 1.4;
}

.sp-holder {
    font-weight: 600;
    color: #e8f5e9;
    flex-shrink: 0;
}

.sp-held {
    flex: 1;
    min-width: 0;
    color: rgba(205, 232, 213, 0.8);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.sp-value {
    flex-shrink: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10px;
    color: #ffd54f;
    opacity: 0.85;
}
</style>
