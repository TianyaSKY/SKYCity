<script lang="ts" setup>
import {ref} from 'vue';
import WorldView from './views/WorldView.vue';
import DashboardView from './views/DashboardView.vue';

const showDashboard = ref(false);
</script>

<template>
    <WorldView v-if="!showDashboard" @open-dashboard="showDashboard = true"/>
    <DashboardView v-else @close="showDashboard = false"/>
</template>
