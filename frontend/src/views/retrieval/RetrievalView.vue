<script setup lang="ts">
/**
 * 混合检索（真实接口）：`POST /api/v1/retrieve`。
 *
 * 只做检索、不做生成：输入问题与召回参数，展示后端返回的结构化引用
 * （文件名 / 分段序号 / 相关度 / 片段 / 可否定位）。
 * 历史演示版里的「三路召回假数据 + 模拟降级开关」已整体移除。
 */
import { ref } from "vue";

import { retrieve, type RetrieveResult } from "@/api/aiops";
import { ApiError } from "@/api/http";
import AppIcon from "@/components/AppIcon.vue";
import { truncate } from "@/lib/format";

const query = ref("");
const topK = ref(15);
const rerankK = ref(5);
const loading = ref(false);
const errorText = ref("");
const result = ref<RetrieveResult | null>(null);

/**
 * 命中里的最高分（用于把进度条归一）。
 *
 * @returns 归一基数，至少 0.0001，避免除零。
 */
function maxScore(): number {
  const scores = (result.value?.documents ?? []).map((item) => item.score);
  return scores.length ? Math.max(...scores, 0.0001) : 1;
}

/** 执行检索。 */
async function run(): Promise<void> {
  if (loading.value) return;
  const text = query.value.trim();
  if (!text) {
    errorText.value = "请输入检索问题";
    return;
  }
  loading.value = true;
  errorText.value = "";
  try {
    result.value = await retrieve({ query: text, topK: topK.value, rerankK: rerankK.value });
  } catch (error) {
    result.value = null;
    errorText.value = error instanceof ApiError ? error.message : "检索失败";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">FishCloud / AIOps / 混合检索</div>
        <h2>混合检索</h2>
        <p>调用后端检索器（向量 + BM25 + 图谱融合）验证召回质量，不经过大模型生成。</p>
      </div>
    </div>

    <div class="filter-bar">
      <label class="fbi grow">
        <span>检索问题</span>
        <input
          v-model="query"
          class="ctl"
          type="search"
          placeholder="输入问题后回车检索"
          @keydown.enter.prevent="run"
        />
      </label>
      <label class="fbi">
        <span>召回条数 topK（{{ topK }}）</span>
        <input v-model.number="topK" class="rng" type="range" min="1" max="50" step="1" />
      </label>
      <label class="fbi">
        <span>重排保留 rerankK（{{ rerankK }}）</span>
        <input v-model.number="rerankK" class="rng" type="range" min="1" max="20" step="1" />
      </label>
      <div class="fb-acts">
        <button class="btn btn-zhu" type="button" :disabled="loading" @click="run">
          <span v-if="loading" class="spin" aria-hidden="true"></span>
          <AppIcon v-else name="search" :size="13" />
          检索
        </button>
      </div>
    </div>

    <div v-if="errorText" class="err-box" role="alert" style="margin-bottom: var(--sp-3)">
      <AppIcon name="events" :size="14" />
      <span style="flex: 1">{{ errorText }}</span>
    </div>

    <div v-if="loading" class="card card--flat">
      <div class="loading-row"><span class="spin" aria-hidden="true"></span>检索中…</div>
    </div>

    <div v-else-if="!result" class="card card--flat">
      <div class="empty">
        <div class="em-ico"><AppIcon name="retrieval" :size="20" /></div>
        尚未检索。输入问题后回车即可查看后端返回的命中片段与相关度。
      </div>
    </div>

    <div v-else-if="!result.documents.length" class="card card--flat">
      <div class="empty">
        <div class="em-ico"><AppIcon name="retrieval" :size="20" /></div>
        没有命中任何资料。可在「知识库」页上传文档后重试。
      </div>
    </div>

    <template v-else>
      <div class="stat-strip cols-3">
        <div class="stat-cell stat-cell--asset">
          <div class="sl">命中条数</div>
          <div class="sv">{{ result.count }}<span class="unit">条</span></div>
          <div class="ss">后端返回的候选数</div>
        </div>
        <div class="stat-cell">
          <div class="sl">最高相关度</div>
          <div class="sv">{{ (result.documents[0]?.score ?? 0).toFixed(4) }}</div>
          <div class="ss">首位命中的融合分数</div>
        </div>
        <div class="stat-cell">
          <div class="sl">可定位来源</div>
          <div class="sv">
            {{ result.documents.filter((doc) => doc.locatable).length }}<span class="unit">/ {{ result.count }}</span>
          </div>
          <div class="ss">带字符坐标、可跳原文的比例</div>
        </div>
      </div>

      <div class="hits">
        <article v-for="(doc, index) in result.documents" :key="doc.chunk_id || index" class="hit-card">
          <header>
            <span class="rk">{{ index + 1 }}</span>
            <span class="fn">{{ doc.filename || doc.doc_id || "（未知来源）" }}</span>
            <span class="tag">#{{ doc.chunk_index }}</span>
            <span v-if="!doc.locatable" class="bdg bdg--warning" :title="doc.degrade_reason">不可定位</span>
            <span class="sc">{{ doc.score.toFixed(4) }}</span>
          </header>
          <div class="bar"><i :style="{ width: `${Math.max(2, (doc.score / maxScore()) * 100)}%` }"></i></div>
          <p class="snip">{{ truncate(doc.snippet || doc.content, 260) }}</p>
          <footer class="meta">
            <span class="mono">chunk {{ doc.chunk_id || "—" }}</span>
            <span v-if="doc.char_start !== null" class="mono">char {{ doc.char_start }}–{{ doc.char_end }}</span>
            <span class="mono">kb {{ doc.kb_id }}</span>
          </footer>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.rng {
  width: 130px;
  accent-color: var(--brand-1);
}
.hits {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.hit-card {
  padding: 11px 14px;
  background-color: var(--zhi-card);
  border: 1px solid var(--bd-1);
  border-radius: var(--r-3);
}
.hit-card header {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 12px;
}
.hit-card .rk {
  flex: 0 0 20px;
  font-family: var(--f-mono);
  font-size: 11px;
  color: var(--mo-4);
}
.hit-card .fn {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
  color: var(--mo-1);
}
.hit-card .tag {
  font-family: var(--f-mono);
  font-size: 10px;
  color: var(--mo-4);
}
.hit-card .sc {
  font-family: var(--f-mono);
  font-size: 12px;
  font-weight: 600;
  color: var(--acc-asset-deep);
  font-variant-numeric: tabular-nums;
}
.hit-card .bar {
  height: 3px;
  margin: 7px 0 8px;
  border-radius: 2px;
  overflow: hidden;
  background-color: var(--zhi-3);
}
.hit-card .bar i {
  display: block;
  height: 100%;
  background-image: linear-gradient(90deg, var(--acc-asset-hover), var(--acc-asset));
  transition: width 0.4s cubic-bezier(0.25, 0.1, 0.25, 1);
}
.hit-card .snip {
  font-size: 11.5px;
  line-height: 1.75;
  color: var(--mo-3);
  word-break: break-word;
}
.hit-card .meta {
  display: flex;
  gap: var(--sp-4);
  flex-wrap: wrap;
  margin-top: 7px;
  color: var(--mo-4);
}
</style>
