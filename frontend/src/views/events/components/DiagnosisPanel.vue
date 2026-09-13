<script setup lang="ts">
/**
 * AI 研判记录展示面板（纯展示 + 单事件，不持有轮询逻辑）。
 *
 * 展示要点（对齐后端约束，绝不美化模型输出）：
 * - 置信度：来自 diagnosis.confidence，可能为 null → 显示「未知」；
 * - 证据编号：evidence[].seq 与答案中的 [E n] 引用一致，逐条列出 kind/ref/snippet；
 * - 降级原因：degraded_reason（timeout / llm_unavailable / insufficient_evidence / unparsed）
 *   以醒目提示呈现，避免用户把降级结论当完整结论；
 * - 失败的研判展示 error 原文，不隐藏。
 */
import { computed } from "vue";

import type { Diagnosis, DiagnosisEvidence } from "@/api/types";
import AppIcon from "@/components/AppIcon.vue";
import {
  DEGRADED_REASON_LABELS,
  DIAGNOSIS_STATUS_BADGE,
  DIAGNOSIS_STATUS_LABELS,
  TRIGGER_TYPE_LABELS,
  labelOf,
} from "@/lib/labels";
import { formatConfidence, formatDateTime, formatLatency } from "@/lib/format";

const props = defineProps<{
  /** 研判记录。 */
  diagnosis: Diagnosis;
  /** 是否为当前选中（高亮）。 */
  active?: boolean;
}>();

const emit = defineEmits<{
  /** 选中该条研判（父组件据此切换详情）。 */
  (event: "select"): void;
}>();

/** 状态是否为进行中。 */
const running = computed(() => props.diagnosis.status === "running");
/** 状态是否为失败/超时。 */
const failed = computed(() => props.diagnosis.status === "failed" || props.diagnosis.status === "timeout");
/** 降级提示文案。 */
const degradedText = computed(() =>
  props.diagnosis.degradedReason ? labelOf(DEGRADED_REASON_LABELS, props.diagnosis.degradedReason) : "",
);
/** 置信度进度条宽度（未知时 0）。 */
const confidenceWidth = computed(() => `${Math.round((props.diagnosis.confidence ?? 0) * 100)}%`);
/** 证据列表（按 seq 升序）。 */
const evidence = computed<DiagnosisEvidence[]>(() =>
  [...(props.diagnosis.evidence ?? [])].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0)),
);

/** 面板附加类（样式区分运行中/失败/降级）。 */
const panelClass = computed(() => ({
  "diag--running": running.value,
  "diag--failed": failed.value,
  "diag--degraded": props.diagnosis.degraded && !failed.value && !running.value,
  "card--lift": props.active === true,
}));
</script>

<template>
  <article class="diag" :class="panelClass" @click="emit('select')">
    <header class="diag-head">
      <span class="bdg" :class="DIAGNOSIS_STATUS_BADGE[diagnosis.status]">
        <span v-if="running" class="d" style="animation: miao 1.6s infinite"></span>
        {{ labelOf(DIAGNOSIS_STATUS_LABELS, diagnosis.status) }}
      </span>
      <span class="bdg bdg--neutral">{{ labelOf(TRIGGER_TYPE_LABELS, diagnosis.triggerType) }}</span>
      <span class="bdg bdg--neutral">#{{ diagnosis.id }}</span>
      <span class="diag-when">{{ formatDateTime(diagnosis.startedAt) }}</span>

      <span class="diag-conf">
        <span class="conf-track" :class="{ low: (diagnosis.confidence ?? 0) < 0.6 }">
          <i :style="{ width: confidenceWidth }"></i>
        </span>
        <span class="conf-val" :title="diagnosis.evidenceSufficient === false ? '证据不充分' : ''">
          {{ formatConfidence(diagnosis.confidence) }}
        </span>
      </span>
    </header>

    <!-- 运行中 -->
    <div v-if="running" class="diag-wait">
      <span class="spin" aria-hidden="true"></span>
      研判执行中，完成后自动刷新。
    </div>

    <template v-else>
      <!-- 失败 -->
      <div v-if="failed" class="err-box" role="alert">
        <AppIcon name="events" :size="14" />
        <span>{{ diagnosis.error || degradedText || "研判未完成" }}</span>
      </div>

      <!-- 降级提示 -->
      <div v-else-if="diagnosis.degraded" class="alert-entry" style="margin-bottom: var(--sp-3)">
        <AppIcon name="events" :size="14" />
        <span>{{ degradedText || "本次研判存在降级" }}</span>
      </div>

      <!-- 根因 -->
      <section class="diag-sec">
        <h4>根因判断</h4>
        <div v-if="diagnosis.rootCause" class="quote">{{ diagnosis.rootCause }}</div>
        <div v-else class="quote quote--empty">
          未给出根因（证据不足时后端不出根因，避免臆造结论）。
        </div>
      </section>

      <!-- 处置建议 -->
      <section v-if="diagnosis.suggestion" class="diag-sec">
        <h4>处置建议</h4>
        <div class="quote quote--sys">{{ diagnosis.suggestion }}</div>
      </section>

      <!-- 证据 -->
      <section class="diag-sec">
        <h4>证据（{{ evidence.length }} 条）</h4>
        <div v-if="!evidence.length" class="diag-note">无证据（平台约束：无证据不出根因）。</div>
        <div v-else class="ev-list">
          <div v-for="item in evidence" :key="item.seq ?? 0" class="ev-item">
            <span class="ev-seq">[E{{ item.seq ?? "-" }}]</span>
            <span class="ev-kind">
              <span class="bdg" :class="item.kind === 'knowledge' ? 'bdg--info' : 'bdg--neutral'">{{
                item.kind === "knowledge" ? "知识库" : "资产"
              }}</span>
            </span>
            <span class="ev-body">
              <span class="ev-title">{{ item.title || "(无标题)" }}</span>
              <span v-if="item.ref" class="cell-sub">{{ item.ref }}</span>
              <span v-if="item.snippet" class="ev-snip">{{ item.snippet }}</span>
            </span>
          </div>
        </div>
      </section>

      <!-- 完整答案（折叠） -->
      <details v-if="diagnosis.answerSummary" class="diag-sec">
        <summary style="cursor: pointer; font-size: 11px; color: var(--mo-3)">模型原始答案</summary>
        <div class="quote quote--sys" style="margin-top: 8px">{{ diagnosis.answerSummary }}</div>
      </details>

      <footer class="diag-meta">
        <span>耗时 {{ formatLatency(diagnosis.latencyMs) }}</span>
        <span>证据充分性 {{ diagnosis.evidenceSufficient === null ? "未知" : diagnosis.evidenceSufficient ? "充分" : "不充分" }}</span>
        <span>完成 {{ formatDateTime(diagnosis.finishedAt) }}</span>
        <span v-if="diagnosis.traceId">链路 {{ diagnosis.traceId }}</span>
      </footer>
    </template>
  </article>
</template>
