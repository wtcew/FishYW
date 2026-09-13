<script setup lang="ts">
/**
 * 效果评估（真实接口）：`POST /api/v1/evaluate` + `GET /api/v1/evaluate/results`。
 *
 * 后端在黄金测试集上跑评估，完成后返回 overall / single_hop / multi_hop 三组指标；
 * 指标键由后端报告决定，因此这里按「键值对」通用渲染，不在前端写死指标名。
 * 历史演示版里的假指标与逐条样本已移除。
 */
import { computed, onUnmounted, ref } from "vue";

import { fetchEvaluateResults, triggerEvaluate, type EvalResult } from "@/api/aiops";
import { ApiError } from "@/api/http";
import AppIcon from "@/components/AppIcon.vue";

/** 轮询间隔（毫秒）与最长次数（3s × 100 ≈ 5 分钟）。 */
const POLL_MS = 3000;
const POLL_MAX = 100;

const result = ref<EvalResult | null>(null);
const loading = ref(false);
const triggering = ref(false);
const errorText = ref("");
const pollTicks = ref(0);
let timer: number | null = null;

/** 后台任务是否在执行。 */
const running = computed(() => result.value?.evaluation_status === "running" || triggering.value);
/** 是否已有可用结果。 */
const hasResult = computed(() => result.value?.status === "ok");

/** 三组指标（键名来自后端）。 */
const metricGroups = computed<Array<{ title: string; data: Record<string, number> }>>(() => {
  const value = result.value;
  if (!value) return [];
  return [
    { title: "总体指标", data: value.overall_metrics ?? {} },
    { title: "单跳样本", data: value.single_hop_metrics ?? {} },
    { title: "多跳样本", data: value.multi_hop_metrics ?? {} },
  ].filter((group) => Object.keys(group.data).length > 0);
});

/** 停止轮询。 */
function stopPoll(): void {
  if (timer !== null) {
    window.clearTimeout(timer);
    timer = null;
  }
}

/** 拉取一次结果（轮询体）。 */
async function poll(): Promise<void> {
  try {
    const data = await fetchEvaluateResults();
    result.value = data;
    if (data.evaluation_status === "running" && pollTicks.value < POLL_MAX) {
      pollTicks.value += 1;
      timer = window.setTimeout(() => void poll(), POLL_MS);
      return;
    }
  } catch (error) {
    errorText.value = error instanceof ApiError ? error.message : "评估结果读取失败";
  }
  stopPoll();
  loading.value = false;
}

/** 读取当前结果（进页面/手动刷新）。 */
async function refresh(): Promise<void> {
  loading.value = true;
  errorText.value = "";
  await poll();
  loading.value = false;
}

/** 触发一次评估。 */
async function trigger(): Promise<void> {
  if (running.value) return;
  triggering.value = true;
  errorText.value = "";
  pollTicks.value = 0;
  try {
    await triggerEvaluate();
    loading.value = true;
    timer = window.setTimeout(() => void poll(), POLL_MS);
  } catch (error) {
    errorText.value = error instanceof ApiError ? error.message : "评估任务启动失败";
    loading.value = false;
  } finally {
    triggering.value = false;
  }
}

onUnmounted(() => stopPoll());
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">FishCloud / AIOps / 效果评估</div>
        <h2>效果评估</h2>
        <p>在黄金测试集上运行 RAGAS 指标评估，结果由后端产出（当前结果保存在服务进程内）。</p>
      </div>
      <div class="ph-actions">
        <button class="btn btn-mo" type="button" :disabled="loading || running" @click="refresh">
          <span v-if="loading" class="spin" aria-hidden="true"></span>
          读取结果
        </button>
        <button class="btn btn-zhu" type="button" :disabled="running" @click="trigger">
          <span v-if="running" class="spin" aria-hidden="true"></span>
          {{ running ? "评估执行中…" : "运行评估" }}
        </button>
      </div>
    </div>

    <div v-if="errorText" class="err-box" role="alert" style="margin-bottom: var(--sp-3)">
      <AppIcon name="events" :size="14" />
      <span style="flex: 1">{{ errorText }}</span>
    </div>

    <div v-if="running" class="alert-entry" style="margin-bottom: var(--sp-3)">
      <span class="spin" aria-hidden="true"></span>
      <span>评估任务在后台执行（黄金测试集逐条推理，耗时取决于模型与样本量），完成后自动刷新。</span>
    </div>

    <div v-if="loading && !result" class="card card--flat">
      <div class="loading-row"><span class="spin" aria-hidden="true"></span>读取中…</div>
    </div>

    <div v-else-if="!hasResult" class="card card--flat">
      <div class="empty">
        <div class="em-ico"><AppIcon name="eval" :size="20" /></div>
        <template v-if="result?.evaluation_status === 'failed'">
          上次评估失败：{{ result.error || "请查看后端日志" }}
        </template>
        <template v-else>尚无评估结果。点击「运行评估」开始，运行期间可离开本页。</template>
      </div>
    </div>

    <template v-else>
      <div class="stat-strip cols-3">
        <div class="stat-cell stat-cell--asset">
          <div class="sl">评估结论</div>
          <div class="sv" :class="result?.passed ? 'ok' : 'warn'">{{ result?.passed ? "通过" : "未通过" }}</div>
          <div class="ss">按后端的通过阈值判定</div>
        </div>
        <div class="stat-cell">
          <div class="sl">样本量</div>
          <div class="sv">{{ result?.sample_count ?? 0 }}<span class="unit">条</span></div>
          <div class="ss">黄金测试集实际执行条数</div>
        </div>
        <div class="stat-cell">
          <div class="sl">指标组</div>
          <div class="sv">{{ metricGroups.length }}<span class="unit">组</span></div>
          <div class="ss">总体 / 单跳 / 多跳</div>
        </div>
      </div>

      <div class="card card--flat" v-for="group in metricGroups" :key="group.title">
        <div class="card-head">
          <div>
            <h3>{{ group.title }}</h3>
            <div class="sub">{{ Object.keys(group.data).length }} 项指标</div>
          </div>
        </div>
        <div class="tbl-wrap">
          <table class="tbl">
            <caption class="sr-only">{{ group.title }}</caption>
            <thead>
              <tr>
                <th scope="col">指标</th>
                <th scope="col" class="ta-r">数值</th>
                <th scope="col">分布</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(value, key) in group.data" :key="key">
                <td class="mono">{{ key }}</td>
                <td class="mono ta-r">{{ Number(value).toFixed(4) }}</td>
                <td>
                  <span class="mbar"><i :style="{ width: `${Math.max(1, Math.min(1, Number(value)) * 100)}%` }"></i></span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.mbar {
  display: block;
  width: 160px;
  height: 5px;
  border-radius: 3px;
  overflow: hidden;
  background-color: var(--zhi-4);
}
.mbar i {
  display: block;
  height: 100%;
  border-radius: 3px;
  background-image: linear-gradient(90deg, var(--acc-asset-hover), var(--acc-asset));
  transition: width 0.5s cubic-bezier(0.25, 0.1, 0.25, 1);
}
</style>
