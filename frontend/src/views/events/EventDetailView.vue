<script setup lang="ts">
/**
 * 事件详情：左侧信息卡与处置动作，右侧 AI 研判记录 + 复盘时间线。
 *
 * 状态流转严格按后端 TRANSITIONS 判定（非法流转后端返回 409，前端先行禁用避免误点）：
 * open→acknowledged|closed；acknowledged→diagnosing|resolved|closed；
 * diagnosing→acknowledged|resolved|closed；resolved→closed；closed 终态。
 * 附加约束（设计稿要求）：P1 严重事件未恢复时禁用「关闭」。
 *
 * AI 研判为异步：POST /events/{id}/diagnose 返回 202 + diagnosis_id，
 * 前端按 2s 间隔轮询 GET /events/diagnoses/{id} 直到 status != running（最长 2 分钟）。
 * 研判历史来自 GET /events/{id}/diagnoses（按开始时间倒序，登录可读）；
 * 按 PRD §5.4 轮询是默认通道，SSE 观察端点属增强，本轮不接。
 */
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { ApiError } from "@/api/http";
import {
  acknowledgeEvent,
  addComment,
  closeEvent,
  getDiagnosis,
  getEvent,
  listEventDiagnoses,
  resolveEvent,
  triggerDiagnosis,
} from "@/api/events";
import { EVENT_TRANSITIONS, type Diagnosis, type EventDetail } from "@/api/types";
import AppIcon from "@/components/AppIcon.vue";
import {
  ENTRY_TYPE_LABELS,
  EVENT_STATUS_BADGE,
  EVENT_STATUS_LABELS,
  SEVERITY_BADGE,
  SEVERITY_LABELS,
  labelOf,
} from "@/lib/labels";
import { formatConfidence, formatDateTime, formatRelative } from "@/lib/format";
import { useAuthStore } from "@/stores/auth";
import { pushToast } from "@/composables/useToast";
import DiagnosisPanel from "./components/DiagnosisPanel.vue";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

/** 轮询间隔与上限（2s × 60 ≈ 2 分钟）。 */
const POLL_INTERVAL_MS = 2000;
const POLL_MAX_TICKS = 60;

const eventId = computed(() => Number(route.params.id));

const detail = ref<EventDetail | null>(null);
const loading = ref(true);
const errorText = ref("");

/** 研判记录列表（新→旧）与当前展开项。 */
const diagnoses = ref<Diagnosis[]>([]);
const selectedDiagnosisId = ref<number | null>(null);

/** 动作状态。 */
const actionPending = ref("");
const actionError = ref("");
const noteText = ref("");
const noteMode = ref<"" | "resolve" | "close">("");

/** 评论。 */
const commentText = ref("");
const commentPending = ref(false);

/** 轮询定时器。 */
let pollTimer: number | null = null;
let pollTicks = 0;

/** 当前状态的合法后继状态。 */
const allowed = computed<string[]>(() => EVENT_TRANSITIONS[detail.value?.status ?? ""] ?? []);
const canAcknowledge = computed(() => auth.can("write") && detail.value?.status === "open");
const canResolve = computed(() => auth.can("write") && allowed.value.includes("resolved"));
/** P1 未恢复时不允许直接关闭（设计稿约束）。 */
const closeBlockedBySeverity = computed(
  () => detail.value?.severity === "critical" && detail.value?.status !== "resolved",
);
const canClose = computed(() => auth.can("write") && allowed.value.includes("closed") && !closeBlockedBySeverity.value);
const canDiagnose = computed(() => auth.can("write") && detail.value?.status !== "closed");
const closed = computed(() => detail.value?.status === "closed");

/** 最新研判（用于左侧摘要）。 */
const latestDiagnosis = computed<Diagnosis | null>(() => diagnoses.value[0] ?? detail.value?.latestDiagnosis ?? null);

/** 当前展开的研判记录。 */
const selectedDiagnosis = computed<Diagnosis | null>(() => {
  if (!diagnoses.value.length) return null;
  const hit = diagnoses.value.find((item) => item.id === selectedDiagnosisId.value);
  return hit ?? diagnoses.value[0] ?? null;
});

/** 研判是否正在进行（按钮禁用与文案）。 */
const diagnosisRunning = computed(() => diagnoses.value.some((item) => item.status === "running"));

/**
 * 研判记录的排序：开始时间倒序（与后端 GET /events/{id}/diagnoses 的次序一致），
 * 同一时刻按 id 兜底；startedAt 缺失（理论不该发生）的记录排到最后。
 *
 * @param a 研判记录。
 * @param b 研判记录。
 * @returns 排序值。
 */
function byStartedDesc(a: Diagnosis, b: Diagnosis): number {
  const ta = a.startedAt ? Date.parse(a.startedAt) : 0;
  const tb = b.startedAt ? Date.parse(b.startedAt) : 0;
  return tb - ta || b.id - a.id;
}

/**
 * 把研判记录并入列表（同 id 覆盖，新记录置顶）。
 *
 * @param record 研判记录。
 */
function upsertDiagnosis(record: Diagnosis): void {
  const rest = diagnoses.value.filter((item) => item.id !== record.id);
  diagnoses.value = [record, ...rest].sort(byStartedDesc);
  selectedDiagnosisId.value = record.id;
}

/** 停止轮询。 */
function stopPoll(): void {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
}

/**
 * 轮询单条研判直到结束。
 *
 * @param id 研判记录 ID。
 */
function pollDiagnosis(id: number): void {
  stopPoll();
  pollTicks = 0;
  const tick = async (): Promise<void> => {
    pollTicks += 1;
    try {
      const record = await getDiagnosis(id);
      upsertDiagnosis(record);
      if (record.status !== "running") {
        stopPoll();
        if (record.status === "completed") {
          pushToast(
            `研判完成：置信度 ${formatConfidence(record.confidence)}，证据 ${record.evidence.length} 条`,
            "success",
          );
        } else {
          pushToast(`研判未完成（${record.status}），事件已回退待人工处置`, "error");
        }
        await loadDetail(true);
        return;
      }
    } catch {
      // 单次轮询失败不中断：网络抖动后继续
    }
    if (pollTicks >= POLL_MAX_TICKS) {
      stopPoll();
      pushToast("研判仍在执行，可稍后刷新页面查看结果", "info");
      return;
    }
    pollTimer = window.setTimeout(() => void tick(), POLL_INTERVAL_MS);
  };
  pollTimer = window.setTimeout(() => void tick(), POLL_INTERVAL_MS);
}

/**
 * 加载事件详情。
 *
 * @param keepSelection 为 true 时保留当前选中的研判记录。
 */
async function loadDetail(keepSelection = false): Promise<void> {
  loading.value = true;
  errorText.value = "";
  try {
    const data = await getEvent(eventId.value);
    detail.value = data;
    await loadDiagnoses(keepSelection);
  } catch (error) {
    detail.value = null;
    errorText.value = error instanceof ApiError ? error.message : "事件加载失败";
  } finally {
    loading.value = false;
  }
}

/**
 * 装配研判历史：GET /events/{id}/diagnoses（按开始时间倒序）。
 *
 * 列表端点不可用时退回详情里的 latestDiagnosis，保证「最近一次结论」这一栏
 * 不会因为列表失败而空掉；详情带的 latestDiagnosis 也一并并入（列表若因
 * 最终一致性还没把它算进去，这里兜住）。
 *
 * @param keepSelection 为 true 时保留当前选中项。
 */
async function loadDiagnoses(keepSelection: boolean): Promise<void> {
  const collected: Diagnosis[] = [];
  try {
    collected.push(...(await listEventDiagnoses(eventId.value)));
  } catch {
    // 历史列表失败不该拖垮整页：降级为「最近一条 + 会话内已轮询到的记录」
  }
  const latest = detail.value?.latestDiagnosis;
  if (latest && !collected.some((item) => item.id === latest.id)) collected.push(latest);

  // 会话内已轮询到的记录不能被覆盖丢失（列表可能还没写入完成态）
  for (const item of diagnoses.value) {
    if (!collected.some((row) => row.id === item.id)) collected.push(item);
  }

  diagnoses.value = collected.sort(byStartedDesc);
  if (!keepSelection || selectedDiagnosisId.value === null) {
    selectedDiagnosisId.value = diagnoses.value[0]?.id ?? null;
  }
}

/** 触发 AI 研判。 */
async function diagnose(): Promise<void> {
  if (actionPending.value || !detail.value) return;
  actionPending.value = "diagnose";
  actionError.value = "";
  try {
    const id = await triggerDiagnosis(eventId.value);
    if (!id) throw new ApiError(0, "后端未返回研判记录 ID");
    pushToast("已受理研判任务，正在执行…", "info");
    await loadDetail(true);
    pollDiagnosis(id);
  } catch (error) {
    actionError.value = error instanceof ApiError ? error.message : "触发研判失败";
  } finally {
    actionPending.value = "";
  }
}

/** 认领事件。 */
async function acknowledge(): Promise<void> {
  if (actionPending.value || !detail.value) return;
  actionPending.value = "acknowledge";
  actionError.value = "";
  try {
    await acknowledgeEvent(detail.value.id);
    pushToast("已认领事件", "success");
    await loadDetail(true);
  } catch (error) {
    actionError.value = error instanceof ApiError ? error.message : "认领失败";
  } finally {
    actionPending.value = "";
  }
}

/**
 * 打开带备注的处置表单。
 *
 * @param mode resolve（恢复）或 close（关闭）。
 */
function openNote(mode: "resolve" | "close"): void {
  noteMode.value = mode;
  noteText.value = "";
  actionError.value = "";
}

/** 提交恢复/关闭。 */
async function submitNote(): Promise<void> {
  if (actionPending.value || !detail.value) return;
  const isResolve = noteMode.value === "resolve";
  if (!noteText.value.trim()) {
    actionError.value = isResolve ? "恢复事件必须填写处置说明" : "关闭事件需填写关闭备注";
    return;
  }
  actionPending.value = isResolve ? "resolve" : "close";
  actionError.value = "";
  try {
    if (isResolve) await resolveEvent(detail.value.id, noteText.value.trim());
    else await closeEvent(detail.value.id, noteText.value.trim());
    pushToast(isResolve ? "已恢复事件" : "已关闭事件", "success");
    noteMode.value = "";
    noteText.value = "";
    await loadDetail(true);
  } catch (error) {
    actionError.value = error instanceof ApiError ? error.message : "操作失败";
  } finally {
    actionPending.value = "";
  }
}

/** 提交复盘评论。 */
async function submitComment(): Promise<void> {
  if (commentPending.value || !detail.value || !commentText.value.trim()) return;
  commentPending.value = true;
  try {
    await addComment(detail.value.id, commentText.value.trim());
    commentText.value = "";
    pushToast("已追加复盘评论", "success");
    await loadDetail(true);
  } finally {
    commentPending.value = false;
  }
}

/**
 * 时间线条目的样式类。
 *
 * @param entryType 条目类型。
 * @param content 条目正文（用于识别研判失败）。
 * @returns CSS 类名。
 */
function entryClass(entryType: string, content: string): Record<string, boolean> {
  const ai = entryType === "diagnosis_started" || entryType === "diagnosis_finished";
  return {
    ai,
    err: entryType === "diagnosis_finished" && content.includes("未完成"),
    run: entryType === "diagnosis_started",
  };
}

// 事件切换（如从时间线点进另一条）时重载
watch(eventId, () => {
  stopPoll();
  diagnoses.value = [];
  selectedDiagnosisId.value = null;
  noteMode.value = "";
  void loadDetail();
});

onMounted(async () => {
  await loadDetail();
  // 页面打开时若已有运行中的研判，继续接管轮询（刷新不丢进度）
  const running = diagnoses.value.find((item) => item.status === "running");
  if (running) pollDiagnosis(running.id);
});

onUnmounted(() => stopPoll());
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">
          FishCloud / 事件中心 / <RouterLink to="/events" class="link">事件与研判</RouterLink> / 详情
        </div>
        <h2>{{ detail?.title ?? "事件详情" }}</h2>
        <p v-if="detail">
          <span class="mono">{{ detail.eventNo }}</span>
          · 来源 {{ detail.source }}
          <span class="ph-count">{{ formatRelative(detail.createdAt) }}发生</span>
        </p>
      </div>
      <div class="ph-actions">
        <button class="btn btn-mo" type="button" @click="router.back()">
          <AppIcon name="back" :size="13" />
          返回
        </button>
        <button
          v-if="canDiagnose"
          class="btn btn-zhu"
          type="button"
          :disabled="actionPending === 'diagnose' || diagnosisRunning"
          @click="diagnose"
        >
          <span v-if="actionPending === 'diagnose' || diagnosisRunning" class="spin" aria-hidden="true"></span>
          <AppIcon v-else name="agent" :size="13" />
          {{ diagnosisRunning ? "研判进行中…" : "触发 AI 研判" }}
        </button>
      </div>
    </div>

    <div v-if="loading && !detail" class="card">
      <div class="loading-row"><span class="spin" aria-hidden="true"></span>加载中…</div>
    </div>

    <div v-else-if="errorText" class="err-box" role="alert">
      <AppIcon name="events" :size="14" />
      <span style="flex: 1">{{ errorText }}</span>
      <button class="btn btn-sm" type="button" @click="loadDetail()">重试</button>
    </div>

    <div v-else-if="detail" class="split split-main-lg">
      <!-- 主列：AI 研判记录 + 复盘时间线（内容主体，占满剩余宽度） -->
      <div class="main-col">
        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>AI 研判记录</h3>
              <div class="sub">证据编号与答案中的 [E n] 引用一致；置信度受证据充分性钳制</div>
            </div>
            <span v-if="diagnoses.length" class="ph-count">{{ diagnoses.length }} 条</span>
          </div>

          <div v-if="!diagnoses.length" class="empty" style="padding: 20px">
            还没有研判记录。触发后这里会展示根因、建议、证据与降级说明。
          </div>
          <template v-else>
            <DiagnosisPanel
              v-for="item in diagnoses"
              :key="item.id"
              :diagnosis="item"
              :active="item.id === selectedDiagnosis?.id"
              @select="selectedDiagnosisId = item.id"
            />
          </template>
        </div>

        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>处置复盘时间线</h3>
              <div class="sub">登记 / 规则命中 / 状态流转 / 研判 / 评论全量留痕</div>
            </div>
            <button class="btn btn-sm btn-mo" type="button" @click="loadDetail(true)">刷新</button>
          </div>

          <div v-if="!detail.timeline.length" class="empty" style="padding: 20px">暂无时间线记录。</div>
          <div v-else class="timeline">
            <div
              v-for="entry in [...detail.timeline].reverse()"
              :key="entry.id"
              class="tl-item"
              :class="entryClass(entry.entryType, entry.content)"
            >
              <div class="tl-head">
                <span class="tt" style="margin: 0">{{ formatDateTime(entry.createdAt) }}</span>
                <span class="tl-tag">{{ labelOf(ENTRY_TYPE_LABELS, entry.entryType) }}</span>
                <span class="tl-who">{{ entry.actorName || entry.actorType }}</span>
              </div>
              <div class="tb">{{ entry.content }}</div>
            </div>
          </div>

          <div v-if="!closed" style="margin-top: var(--sp-4)">
            <div class="field">
              <label for="ev-comment">追加复盘评论</label>
              <textarea
                id="ev-comment"
                v-model="commentText"
                class="ctl"
                rows="2"
                maxlength="2000"
                placeholder="记录处置动作、现场现象或后续观察项"
                :disabled="commentPending || !auth.can('write')"
              ></textarea>
            </div>
            <div style="display: flex; justify-content: flex-end; margin-top: 8px">
              <button
                class="btn btn-sm btn-zhu"
                type="button"
                :disabled="commentPending || !commentText.trim() || !auth.can('write')"
                @click="submitComment"
              >
                <span v-if="commentPending" class="spin" aria-hidden="true"></span>
                提交评论
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- 元信息列（信息卡 + 处置动作）：主次分明，滚动主列时保持可见 -->
      <div class="side-meta">
        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>事件信息</h3>
              <div class="sub">{{ detail.assetName || "未关联资产" }}</div>
            </div>
            <span class="bdg" :class="EVENT_STATUS_BADGE[detail.status]">
              {{ labelOf(EVENT_STATUS_LABELS, detail.status) }}
            </span>
          </div>

          <div class="kv">
            <div class="k">严重度</div>
            <div class="v">
              <span class="bdg bdg--sev" :class="SEVERITY_BADGE[detail.severity]">
                {{ labelOf(SEVERITY_LABELS, detail.severity) }}
              </span>
            </div>
          </div>
          <div class="kv">
            <div class="k">事件编号</div>
            <div class="v mono">{{ detail.eventNo }}</div>
          </div>
          <div class="kv">
            <div class="k">来源</div>
            <div class="v mono">{{ detail.source }}</div>
          </div>
          <div class="kv">
            <div class="k">影响资产</div>
            <div class="v">
              <template v-if="detail.assetId">
                <RouterLink :to="`/assets/${detail.assetId}`" class="link">
                  {{ detail.assetName || `资产 #${detail.assetId}` }}
                </RouterLink>
                <div v-if="detail.environmentName" class="cell-sub">
                  {{ detail.businessLineName }} / {{ detail.environmentName }}
                </div>
              </template>
              <span v-else class="dim">未关联</span>
            </div>
          </div>
          <div class="kv">
            <div class="k">认领人</div>
            <div class="v">{{ detail.acknowledgedByName || "尚未认领" }}</div>
          </div>
          <div class="kv">
            <div class="k">发生时间</div>
            <div class="v mono">{{ formatDateTime(detail.createdAt) }}</div>
          </div>
          <div class="kv">
            <div class="k">恢复时间</div>
            <div class="v mono">{{ detail.resolvedAt ? formatDateTime(detail.resolvedAt) : "—" }}</div>
          </div>

          <div class="diag-sec">
            <h4>最新 AI 研判</h4>
            <div v-if="latestDiagnosis?.rootCause" class="quote">{{ latestDiagnosis.rootCause }}</div>
            <div v-else-if="latestDiagnosis?.status === 'running'" class="quote quote--empty">研判执行中…</div>
            <div v-else-if="latestDiagnosis?.error" class="quote quote--empty">{{ latestDiagnosis.error }}</div>
            <div v-else class="quote quote--empty">
              尚无研判结论。
            </div>
          </div>
        </div>

        <!-- 处置动作 -->
        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>处置动作</h3>
              <div class="sub">状态机：{{ detail.status }} → {{ allowed.length ? allowed.join(" / ") : "终态" }}</div>
            </div>
          </div>

          <div v-if="closed" class="empty" style="padding: 16px">事件已关闭（终态），不可再流转。</div>
          <template v-else>
            <div style="display: flex; flex-wrap: wrap; gap: 6px">
              <button
                class="btn btn-sm"
                type="button"
                :disabled="!canAcknowledge || actionPending === 'acknowledge'"
                @click="acknowledge"
              >
                <span v-if="actionPending === 'acknowledge'" class="spin" aria-hidden="true"></span>
                认领
              </button>
              <button
                class="btn btn-sm btn-zhu"
                type="button"
                :disabled="!canResolve"
                @click="openNote('resolve')"
              >
                恢复（需说明）
              </button>
              <button
                class="btn btn-sm btn-mo"
                type="button"
                :disabled="!canClose"
                :title="closeBlockedBySeverity ? 'P1 严重事件需先恢复再关闭' : ''"
                @click="openNote('close')"
              >
                关闭
              </button>
            </div>
            <p v-if="closeBlockedBySeverity" class="ctl-hint" style="margin-top: 8px">
              P1 严重事件在恢复前不允许直接关闭，请先填写处置说明并恢复。
            </p>
            <p v-if="!auth.can('write')" class="ctl-hint" style="margin-top: 8px">
              当前角色（{{ auth.roleName || auth.role }}）为只读，无法执行流转。
            </p>

            <div v-if="noteMode" style="margin-top: var(--sp-4)">
              <div class="field">
                <label for="ev-note">{{ noteMode === "resolve" ? "处置说明（必填）" : "关闭备注（必填）" }}</label>
                <textarea
                  id="ev-note"
                  v-model="noteText"
                  class="ctl"
                  rows="3"
                  maxlength="1000"
                  :placeholder="noteMode === 'resolve' ? '如：清理风道滤网后温度回落至 52℃，持续观察 30 分钟无异常' : '如：根因已在变更单 CHG-1024 中闭环'"
                  :disabled="actionPending !== ''"
                ></textarea>
              </div>
              <div style="display: flex; gap: 6px; justify-content: flex-end; margin-top: 8px">
                <button class="btn btn-sm btn-ghost" type="button" @click="noteMode = ''">取消</button>
                <button class="btn btn-sm btn-zhu" type="button" :disabled="actionPending !== ''" @click="submitNote">
                  <span v-if="actionPending !== ''" class="spin" aria-hidden="true"></span>
                  提交
                </button>
              </div>
            </div>

            <p v-if="actionError" class="ctl-err" role="alert" style="margin-top: var(--sp-3)">{{ actionError }}</p>
          </template>
        </div>
      </div>
    </div>
  </section>
</template>
