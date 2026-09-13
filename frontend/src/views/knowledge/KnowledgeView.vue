<script setup lang="ts">
// 知识库管理：文档级列表、上传、删除与分段查看。
// 与检索同源：列表里的 chunks 由后端实时聚合，删除后 BM25 同步重建，
// 因此界面看到的就是检索实际能命中的内容，不存在两套口径。
import { computed, onMounted, onUnmounted, ref } from "vue";

import { authHeaders, handleUnauthorized } from "@/api/http";

interface KbDocument {
  doc_id: string;
  kb_id: string;
  filename: string;
  file_type: string;
  chunks: number;
  chars: number;
  char_length: number;
  /** 后端标记：早期入库、缺少溯源字段的文档（模板据此显示「历史数据」徽标）。 */
  legacy?: boolean;
}

interface KbChunk {
  chunk_id: string;
  chunk_index: number;
  filename: string;
  char_start: number | null;
  char_end: number | null;
  locatable: boolean;
  degrade_reason: string;
  content: string;
}

interface IngestJob {
  doc_id: string;
  filename: string;
  status: "queued" | "running" | "success" | "failed" | "cancelled";
  stage: string;
  progress: number;
  error: string | null;
  chunk_count: number;
  entity_count: number;
  terminal: boolean;
}

// 阶段必须说人话：用户投诉「文件卡在 Queuing」，根因是界面只有转圈、没有阶段，
// 把后端枚举原样抛出去等于什么都没说。
const STAGE_TEXT: Record<string, string> = {
  queued: "排队中",
  parse: "解析文档",
  chunk: "切分段落",
  embed: "向量化",
  entity: "抽取实体",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const documents = ref<KbDocument[]>([]);
const jobs = ref<IngestJob[]>([]);
const totalChunks = ref(0);
const loading = ref(false);
const busy = ref(false);
const message = ref("");
const error = ref("");
const openDocId = ref("");
const chunksOf = ref<Record<string, KbChunk[]>>({});
let pollTimer: number | null = null;
let doneSeen = 0;

const apiBase = computed(() => "/api/v1");

/**
 * 带鉴权的知识库请求（AIOps 接口现已要求登录，2026-09-13 安全加固）。
 *
 * FormData 上传不设 Content-Type（浏览器需自行补 boundary）；401 统一走
 * handleUnauthorized（清 token → 跳登录），与 http.ts 的行为一致。
 */
async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(apiBase.value + path, {
    ...init,
    headers: { ...authHeaders(), ...((init.headers as Record<string, string>) ?? {}) },
  });
  if (res.status === 401) handleUnauthorized();
  return res;
}

// 只展示还需要用户关注的任务：进行中的要有进度，失败/取消的要有重试入口。
// 成功的任务已经出现在下面的文档表里，再挂一行纯属噪音。
const activeJobs = computed(() =>
  jobs.value.filter((job) => !job.terminal || job.status === "failed" || job.status === "cancelled")
);

function stageText(job: IngestJob): string {
  return STAGE_TEXT[job.stage] ?? (job.stage || "处理中");
}

function percent(job: IngestJob): number {
  return Math.max(2, Math.min(100, Math.round(job.progress * 100)));
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const res = await authedFetch("/knowledge/documents");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = (await res.json()) as { documents: KbDocument[]; total_chunks: number };
    documents.value = data.documents ?? [];
    totalChunks.value = data.total_chunks ?? 0;
    error.value = "";
  } catch (err) {
    error.value = "无法读取知识库：" + (err as Error).message + "（请确认后端已启动）";
  } finally {
    loading.value = false;
  }
}

async function refreshJobs(): Promise<void> {
  try {
    const res = await authedFetch("/knowledge/jobs");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = (await res.json()) as { jobs: IngestJob[] };
    jobs.value = data.jobs ?? [];
    const doneNow = jobs.value.filter((job) => job.status === "success").length;
    if (doneNow !== doneSeen) {
      // 有任务刚落地：文档列表要跟着变，否则用户会以为上传丢了。
      doneSeen = doneNow;
      await refresh();
    }
    schedule();
  } catch {
    // 任务表读不到不影响主流程：文档列表本身仍然可用。
  }
}

// 只在确实有任务在跑时开轮询，跑完立刻停——空闲时不该有后台请求。
function schedule(): void {
  const pending = jobs.value.some((job) => !job.terminal);
  if (pending && pollTimer === null) {
    pollTimer = window.setInterval(() => void refreshJobs(), 1000);
  } else if (!pending && pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

// 手动刷新要连任务表一起刷新：任务是文档的来源，只刷一半会让人以为上传丢了。
async function refreshAll(): Promise<void> {
  await Promise.all([refresh(), refreshJobs()]);
}

onMounted(() => {
  void refresh();
  void refreshJobs();
});

onUnmounted(() => {
  if (pollTimer !== null) window.clearInterval(pollTimer);
  pollTimer = null;
});

function pickFiles(): void {
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = true;
  input.accept = ".md,.txt,.pdf,.docx";
  input.onchange = () => {
    const files = Array.from(input.files ?? []);
    if (files.length) void upload(files);
  };
  input.click();
}

async function upload(files: File[]): Promise<void> {
  busy.value = true;
  message.value = "正在解析并向量化 " + files.length + " 个文件…";
  error.value = "";
  try {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    const res = await authedFetch("/upload", { method: "POST", body: form });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error("HTTP " + res.status + " " + detail.slice(0, 120));
    }
    // 后端受理即返回（202）：这里把任务挂到界面上，进度交给轮询。
    // 用户不必再对着一个没有反馈的按钮等到解析结束。
    const data = (await res.json()) as { status: string; jobs: IngestJob[] };
    message.value = "已受理 " + (data.jobs ?? []).length + " 个文件，正在后台解析…";
    (data.jobs ?? []).forEach((job) => {
      const idx = jobs.value.findIndex((item) => item.doc_id === job.doc_id);
      if (idx >= 0) jobs.value[idx] = job;
      else jobs.value = [job, ...jobs.value];
    });
    schedule();
    await refreshJobs();
  } catch (err) {
    error.value = "上传失败：" + (err as Error).message;
    message.value = "";
  } finally {
    busy.value = false;
  }
}

async function retry(job: IngestJob): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    const res = await authedFetch("/knowledge/jobs/" + job.doc_id + "/retry", {
      method: "POST",
    });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error("HTTP " + res.status + " " + detail.slice(0, 120));
    }
    message.value = "已重新排队：" + job.filename;
    await refreshJobs();
  } catch (err) {
    error.value = "重试失败：" + (err as Error).message;
  } finally {
    busy.value = false;
  }
}

async function cancel(job: IngestJob): Promise<void> {
  busy.value = true;
  try {
    const res = await authedFetch("/knowledge/jobs/" + job.doc_id + "/cancel", {
      method: "POST",
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    message.value = "已取消：" + job.filename;
    await refreshJobs();
  } catch (err) {
    error.value = "取消失败：" + (err as Error).message;
  } finally {
    busy.value = false;
  }
}

async function remove(doc: KbDocument): Promise<void> {
  if (!window.confirm("删除《" + doc.filename + "》及其 " + doc.chunks + " 个分段？")) return;
  busy.value = true;
  try {
    const res = await authedFetch("/knowledge/documents/" + doc.doc_id, { method: "DELETE" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = (await res.json()) as { removed_chunks: number };
    message.value = "已删除 " + data.removed_chunks + " 个分段";
    if (openDocId.value === doc.doc_id) openDocId.value = "";
    await refresh();
  } catch (err) {
    error.value = "删除失败：" + (err as Error).message;
  } finally {
    busy.value = false;
  }
}

async function toggleChunks(doc: KbDocument): Promise<void> {
  if (openDocId.value === doc.doc_id) {
    openDocId.value = "";
    return;
  }
  openDocId.value = doc.doc_id;
  if (chunksOf.value[doc.doc_id]) return;
  try {
    const res = await authedFetch("/knowledge/documents/" + doc.doc_id + "/chunks");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = (await res.json()) as { chunks: KbChunk[] };
    chunksOf.value = { ...chunksOf.value, [doc.doc_id]: data.chunks ?? [] };
  } catch (err) {
    error.value = "读取分段失败：" + (err as Error).message;
  }
}

function shortText(text: string, limit = 160): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > limit ? clean.slice(0, limit) + "…" : clean;
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">Module / Knowledge</div>
        <h2>知识库</h2>
        <p>上传的资料会立即向量化并参与检索，与服务端检索口径一致</p>
      </div>
      <div class="ph-actions">
        <button class="btn" :disabled="loading" @click="refreshAll()">刷新</button>
        <button class="btn btn-zhu" :disabled="busy" @click="pickFiles()">
          {{ busy ? "处理中…" : "上传文档" }}
        </button>
      </div>
    </div>

    <div class="stats">
      <div class="stat"><span class="n">{{ documents.length }}</span><span class="l">文档</span></div>
      <div class="stat"><span class="n">{{ totalChunks }}</span><span class="l">分段</span></div>
      <div class="stat"><span class="n">{{ documents.reduce((s, d) => s + d.chars, 0).toLocaleString() }}</span><span class="l">入库字符</span></div>
    </div>

    <div v-if="message" class="msg ok">{{ message }}</div>
    <div v-if="error" class="msg err">{{ error }}</div>

    <div v-if="activeJobs.length" class="card jobs-card">
      <div class="card-head"><div><h3>解析任务</h3>
        <div class="sub">上传后在这里看进度，失败可直接重试，不必重新选文件</div></div></div>
      <div class="jobs">
        <div v-for="job in activeJobs" :key="job.doc_id" class="job" :class="job.status">
          <div class="job-main">
            <div class="job-name">{{ job.filename }}</div>
            <div class="job-meta">
              <span class="stage">{{ stageText(job) }}</span>
              <span v-if="!job.terminal" class="pct mono">{{ percent(job) }}%</span>
              <span v-if="job.error" class="job-err" :title="job.error">{{ job.error }}</span>
            </div>
          </div>
          <div v-if="!job.terminal" class="bar"><i :style="{ width: percent(job) + '%' }"></i></div>
          <div class="job-acts">
            <button v-if="!job.terminal" class="act" :disabled="busy" @click="cancel(job)">取消</button>
            <button v-if="job.status === 'failed' || job.status === 'cancelled'" class="act" :disabled="busy" @click="retry(job)">重试</button>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><div><h3>文档</h3>
        <div class="sub">支持 .md / .txt / .pdf / .docx，全程内存处理</div></div></div>
      <div v-if="!documents.length" class="empty">
        还没有文档。点击右上角「上传文档」把运维手册、故障 SOP、维修记录加进来。
      </div>
      <div v-else class="tbl-wrap">
        <table class="tbl">
          <thead><tr><th>文件名</th><th>类型</th><th>分段</th><th>入库字符</th><th>操作</th></tr></thead>
          <tbody>
            <template v-for="doc in documents" :key="doc.doc_id">
              <tr :class="{ open: openDocId === doc.doc_id }">
                <td class="fname">
                  {{ doc.filename }}
                  <span v-if="doc.legacy" class="legacy" title="早期入库、缺少溯源字段，建议重新上传以支持引用定位">历史数据</span>
                </td>
                <td class="mono">{{ doc.file_type }}</td>
                <td class="mono">{{ doc.chunks }}</td>
                <td class="mono">{{ doc.chars.toLocaleString() }}</td>
                <td>
                  <button class="act" @click="toggleChunks(doc)">
                    {{ openDocId === doc.doc_id ? "收起分段" : "查看分段" }}
                  </button>
                  <button class="act danger" :disabled="busy" @click="remove(doc)">删除</button>
                </td>
              </tr>
              <tr v-if="openDocId === doc.doc_id" class="chunks-row">
                <td colspan="5">
                  <div v-for="c in chunksOf[doc.doc_id] ?? []" :key="c.chunk_id" class="chunk">
                    <div class="chunk-head">
                      <span class="idx">#{{ c.chunk_index }}</span>
                      <span v-if="c.locatable" class="range mono">原文 {{ c.char_start }}–{{ c.char_end }}</span>
                      <span v-else class="range warn">无字符坐标 · {{ c.degrade_reason }}</span>
                    </div>
                    <div class="chunk-body">{{ shortText(c.content, 220) }}</div>
                  </div>
                  <div v-if="!(chunksOf[doc.doc_id] ?? []).length" class="empty small">该文档暂无分段</div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<style scoped>
.stats{display:flex;gap:28px;padding:0 0 18px}
.stat{display:flex;align-items:baseline;gap:7px}
.stat .n{font-family:var(--f-mono);font-size:20px;font-weight:600;color:var(--mo-1)}
.stat .l{font-size:11px;color:var(--mo-4)}
.msg{font-size:11.5px;padding:7px 11px;border-radius:var(--r-1);margin-bottom:12px}
.msg.ok{color:var(--song-1);background:rgba(62,92,74,.07);border:1px solid rgba(62,92,74,.2)}
.msg.err{color:var(--st-danger-text);background:rgba(var(--danger-rgb),.06);border:1px solid rgba(var(--danger-rgb),.22)}
.empty{padding:26px 0;font-size:12px;color:var(--mo-4);line-height:1.8}
.empty.small{padding:10px 0;font-size:11.5px}
.fname{color:var(--mo-1)}
.jobs-card{margin-bottom:16px}
.jobs{display:flex;flex-direction:column;padding:0 14px 12px}
.job{padding:9px 0;border-bottom:1px solid var(--bian-2)}
.job:last-child{border-bottom:none}
.job-main{display:flex;align-items:baseline;gap:14px;justify-content:space-between}
.job-name{font-size:12px;color:var(--mo-1)}
.job-meta{display:flex;gap:12px;align-items:baseline;font-size:11px;color:var(--mo-4);min-width:0}
.job-meta .stage{color:var(--dian-1);white-space:nowrap}
.job.failed .job-meta .stage,.job.cancelled .job-meta .stage{color:var(--st-danger-text)}
.pct{font-size:10.5px;color:var(--mo-4)}
.job-err{color:var(--st-danger-text);max-width:46ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{height:2px;background:var(--zhi-3);margin-top:7px;border-radius:2px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--dian-1);transition:width .3s ease}
.job-acts{display:flex;gap:12px;justify-content:flex-end;margin-top:5px}
@media (prefers-reduced-motion:reduce){.bar i{transition:none}}
/* 历史数据标记：这类记录没有 doc_id，只能靠文件名兜底，引用定位不可用。 */
.legacy{margin-left:7px;font-size:9.5px;color:var(--teng-3);border:1px solid rgba(184,134,11,.32);border-radius:2px;padding:0 4px;white-space:nowrap}
tr.open td{background:var(--zhi-3)}
.act{border:none;background:none;padding:0;margin-right:12px;font-family:var(--f-hei);font-size:11.5px;color:var(--mo-3);cursor:pointer}
.act:hover{color:var(--zhu-1)}
.act:disabled{color:var(--mo-5);cursor:not-allowed}
.act.danger:hover{color:var(--st-danger-text)}
.chunks-row td{padding:12px 14px;background:var(--zhi-3)}
.chunk{border-left:2px solid var(--bian-2);padding:6px 0 6px 11px;margin-bottom:9px}
.chunk-head{display:flex;gap:12px;align-items:baseline;margin-bottom:4px}
.idx{font-family:var(--f-mono);font-size:10.5px;color:var(--zhu-1)}
.range{font-size:10.5px;color:var(--mo-4)}
.range.warn{color:var(--teng-3)}
.chunk-body{font-size:11.5px;color:var(--mo-2);line-height:1.75}
</style>