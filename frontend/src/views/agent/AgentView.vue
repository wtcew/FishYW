<script setup lang="ts">
/**
 * Agent 链路：真实链路明细查询 + 状态机结构说明。
 *
 * - 链路明细：`GET /api/v1/trace/{trace_id}`（内存态，最多保留 200 条，服务重启即失效）。
 *   trace_id 来自问答/研判结果（问答页每次回答后会给出）。
 * - 状态机结构：LangGraph 的 5 个节点名取自后端 `src/agent/state_machine.py`，
 *   仅作结构说明，不含任何模拟耗时或演示动画。
 * 历史演示版里的假节点耗时与自动播放动画已移除。
 */
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";

import { fetchTrace, type TraceRecord } from "@/api/aiops";
import { ApiError } from "@/api/http";
import AppIcon from "@/components/AppIcon.vue";
import { truncate } from "@/lib/format";

/** 真实节点（顺序与 state_machine.py 的 add_node / 边一致）。 */
const NODES: Array<{ key: string; title: string; desc: string }> = [
  { key: "decompose", title: "意图拆解", desc: "把问题拆成子问题，便于分别召回" },
  { key: "retrieve", title: "混合检索", desc: "向量 + BM25 + 图谱融合召回候选片段" },
  { key: "reflect", title: "充分性反思", desc: "判断证据是否足够，不足则回到拆解重试" },
  { key: "generate", title: "答案生成", desc: "以检索到的资料为上下文生成回答" },
  { key: "hallucination_check", title: "幻觉校验", desc: "校验答案是否脱离资料，必要时重生成" },
];

const route = useRoute();
const traceId = ref(typeof route.query.traceId === "string" ? route.query.traceId : "");
const record = ref<TraceRecord | null>(null);
const loading = ref(false);
const errorText = ref("");

/** 查询链路明细。 */
async function lookup(): Promise<void> {
  const id = traceId.value.trim();
  if (!id) {
    errorText.value = "请输入 trace_id";
    return;
  }
  loading.value = true;
  errorText.value = "";
  record.value = null;
  try {
    record.value = await fetchTrace(id);
  } catch (error) {
    errorText.value = error instanceof ApiError ? error.message : "链路查询失败";
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  if (traceId.value) void lookup();
});
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">FishCloud / AIOps / Agent 链路</div>
        <h2>Agent 链路</h2>
        <p>查一次问答/研判的完整链路明细（问题、答案、引用来源、迭代次数与耗时）。</p>
      </div>
    </div>

    <div class="split split-main">
      <!-- 主列：链路明细 -->
      <div class="main-col">
        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>链路明细</h3>
              <div class="sub">trace_id 由问答与研判结果返回；明细保存在服务进程内</div>
            </div>
          </div>

          <div class="filter-bar" style="border-bottom: none; padding-bottom: 0; margin-bottom: var(--sp-3)">
            <label class="fbi grow">
              <span>trace_id</span>
              <input
                v-model="traceId"
                class="ctl mono"
                type="search"
                placeholder="如 1762900000000-12"
                @keydown.enter.prevent="lookup"
              />
            </label>
            <div class="fb-acts">
              <button class="btn btn-zhu" type="button" :disabled="loading" @click="lookup">
                <span v-if="loading" class="spin" aria-hidden="true"></span>
                <AppIcon v-else name="search" :size="13" />
                查询
              </button>
            </div>
          </div>

          <div v-if="errorText" class="err-box" role="alert">
            <span style="flex: 1">{{ errorText }}</span>
          </div>

          <div v-else-if="loading" class="loading-row"><span class="spin" aria-hidden="true"></span>查询中…</div>

          <div v-else-if="!record" class="empty" style="padding: 24px">
            输入 trace_id 查询链路明细。
          </div>

          <div v-else-if="!record.found" class="empty" style="padding: 24px">
            未找到该 trace_id 的链路记录（明细仅保留最近 200 条，服务重启后清空）。
          </div>

          <template v-else>
            <div class="kv">
              <div class="k">trace_id</div>
              <div class="v mono">{{ record.trace_id }}</div>
            </div>
            <div class="kv">
              <div class="k">问题</div>
              <div class="v">{{ record.query || "—" }}</div>
            </div>
            <div class="kv">
              <div class="k">迭代次数</div>
              <div class="v mono">{{ record.iterations ?? 0 }}</div>
            </div>
            <div class="kv">
              <div class="k">总耗时</div>
              <div class="v mono">
                {{ record.elapsed_ms !== undefined ? `${(record.elapsed_ms / 1000).toFixed(2)}s` : "—" }}
              </div>
            </div>
            <div class="kv">
              <div class="k">错误</div>
              <div class="v">
                <span v-if="record.error" class="bdg bdg--danger">{{ record.error }}</span>
                <span v-else class="t-dim">无</span>
              </div>
            </div>

            <div class="diag-sec">
              <h4>答案</h4>
              <div v-if="record.answer" class="quote">{{ record.answer }}</div>
              <div v-else class="quote quote--empty">该次调用没有产出答案。</div>
            </div>

            <div class="diag-sec">
              <h4>引用来源（{{ record.sources?.length ?? 0 }} 条）</h4>
              <div v-if="!record.sources?.length" class="diag-note">本次调用没有返回引用来源。</div>
              <div v-else class="ev-list">
                <div v-for="(item, index) in record.sources" :key="item.chunk_id || index" class="ev-item">
                  <span class="ev-seq">[{{ index + 1 }}]</span>
                  <span class="ev-body">
                    <span class="ev-title">{{ item.filename || item.doc_id || "（未知来源）" }}</span>
                    <span v-if="!item.locatable" class="bdg bdg--warning" style="margin-left: 6px">不可定位</span>
                    <span class="ev-snip">{{ truncate(item.snippet || item.content, 200) }}</span>
                  </span>
                  <!-- /trace 的 sources 不保证带 score（后端仅回 chunk_id/filename/chunk_index），
                       直接 toFixed 会在渲染期抛异常并卡死页面（2026-09-13 UI 穷举测试 P1）。 -->
                  <span class="mono t-dim">{{ typeof item.score === "number" ? item.score.toFixed(4) : "—" }}</span>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>

      <!-- 元信息列：状态机结构（真实节点名，非模拟） -->
      <div class="side-meta">
        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>状态机节点</h3>
              <div class="sub">后端 LangGraph 的真实节点与顺序</div>
            </div>
          </div>
          <ol class="nodes">
            <li v-for="(node, index) in NODES" :key="node.key">
              <span class="idx">{{ index + 1 }}</span>
              <div>
                <b>{{ node.title }}</b>
                <span class="fn mono">{{ node.key }}</span>
                <p>{{ node.desc }}</p>
              </div>
            </li>
          </ol>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.nodes {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.nodes li {
  display: flex;
  gap: 9px;
  align-items: flex-start;
}
.nodes .idx {
  flex: 0 0 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--bd-2);
  border-radius: var(--r-1);
  font-family: var(--f-mono);
  font-size: 10px;
  color: var(--mo-3);
  background-color: var(--zhi-3);
}
.nodes b {
  font-size: 12px;
  color: var(--mo-1);
  font-weight: 600;
}
.nodes .fn {
  margin-left: 6px;
  font-size: 10px;
  color: var(--mo-4);
}
.nodes p {
  margin-top: 3px;
  font-size: 11px;
  color: var(--mo-3);
  line-height: 1.65;
}
</style>
