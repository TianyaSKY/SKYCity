<script lang="ts" setup>
import {computed} from 'vue';
import {SHIFT_STATUS_LABELS, useWorldStore} from '../stores/worldStore';

/**
 * 居民身份卡扩展 (M13): 正式职业、企业、岗位、每班工资、下一班次、
 * 出勤评分、欠薪与最近班次记录。数据来自 store 的 employment REST 缓存
 * 与 WS 班次事件缓存 (agentEmployment / agentShifts / jobOpenings)。
 */

const props = defineProps<{ agentId: string }>();

const store = useWorldStore();

function timeOf(worldTime: number | null | undefined): string {
    if (worldTime == null) return '—';
    const hours = Math.floor(worldTime / 60) % 24;
    const minutes = worldTime % 60;
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
}

const employment = computed(() => store.employmentOf(props.agentId));
const nextShift = computed(() => store.nextShiftOf(props.agentId));
const shifts = computed(() => store.agentEmployment[props.agentId]?.shifts ?? []);
const companyName = computed(() =>
    employment.value
        ? (store.companyById(employment.value.company_id)?.name ?? employment.value.company_id)
        : '',
);
/** 岗位中文名: 优先公开招聘列表里的 title, 回退到 job_id。 */
const positionTitle = computed(() => {
    const emp = employment.value;
    if (!emp) return '';
    return store.jobOpenings.find((o) => o.position_id === emp.position_id)?.title ?? emp.job_id;
});

function shiftStatusLabel(status: string): string {
    return SHIFT_STATUS_LABELS[status] ?? status;
}
</script>

<template>
    <div class="career-card">
        <template v-if="employment">
            <div class="career-head">
                <span class="career-company">{{ companyName }}</span>
                <span class="career-position">{{ positionTitle }}</span>
            </div>
            <div class="career-row">
                <span>每班工资 <b>{{ employment.wage_per_shift }}</b> 金币</span>
                <span>出勤评分 <b>{{ employment.attendance_score }}</b></span>
            </div>
            <div class="career-row">
                <span>欠薪 <b class="warn">{{ employment.unpaid_wage }}</b> 金币</span>
                <span>已完成 <b>{{ employment.completed_shifts }}</b> 班</span>
            </div>
            <div class="career-next">
                下一班次：<b>{{
                    nextShift ? `${timeOf(nextShift.scheduled_start)} – ${timeOf(nextShift.scheduled_end)}` : '暂无'
                }}</b>
            </div>
            <div v-if="shifts.length > 0" class="career-shifts">
                <div class="career-shift-title">最近班次</div>
                <div v-for="s in shifts.slice(0, 5)" :key="s.shift_id" class="career-shift">
                    <span class="cs-time">{{ timeOf(s.scheduled_start) }}</span>
                    <span :class="`st-${s.status}`" class="cs-status">{{ shiftStatusLabel(s.status) }}</span>
                    <span v-if="s.wage_paid > 0" class="cs-wage">+{{ s.wage_paid }}</span>
                    <span v-else-if="s.status === 'completed'" class="cs-wage due">欠 {{ s.wage_due }}</span>
                </div>
            </div>
        </template>
        <p v-else class="career-empty">暂无正式职业</p>
    </div>
</template>

<style scoped>
.career-card {
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    background: rgba(255, 255, 255, 0.04);
    padding: 10px;
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.career-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
}

.career-company {
    font-weight: 600;
    color: #ffe082;
    font-size: 13px;
}

.career-position {
    color: rgba(205, 232, 213, 0.8);
    font-size: 11.5px;
}

.career-row {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    color: rgba(205, 232, 213, 0.85);
    font-size: 11.5px;
}

.career-row b {
    color: #e8f5e9;
}

.career-row b.warn {
    color: #ff8a65;
}

.career-next {
    color: rgba(205, 232, 213, 0.85);
    font-size: 11.5px;
}

.career-next b {
    color: #e8f5e9;
}

.career-shift-title {
    margin-top: 2px;
    color: rgba(205, 232, 213, 0.5);
    font-size: 11px;
}

.career-shift {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 11.5px;
    line-height: 1.5;
}

.cs-time {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    color: rgba(205, 232, 213, 0.6);
    flex-shrink: 0;
}

.cs-status {
    flex: 1;
    color: rgba(205, 232, 213, 0.85);
}

.cs-status.st-in_progress,
.cs-status.st-late {
    color: #ffd54f;
}

.cs-status.st-completed {
    color: #9fd8ae;
}

.cs-status.st-absent,
.cs-status.st-cancelled {
    color: #ff8a65;
}

.cs-status.st-leave {
    color: #90caf9;
}

.cs-wage {
    color: #9fd8ae;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
}

.cs-wage.due {
    color: #ff8a65;
}

.career-empty {
    margin: 0;
    padding: 12px 4px;
    text-align: center;
    color: rgba(205, 232, 213, 0.55);
}
</style>
