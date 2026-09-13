<script setup lang="ts">
// 智能问答：优先直连用户配置的大模型（设置里选服务商 + 填 Key 即可），
// 否则调用后端 /api/v1/diagnose（SSE 流式）。两条路径都不可用时明确报错，不做本地兜底。
// 会话（多对话）与消息历史整体持久化，支持回顾、重命名、删除、重新生成与多轮上下文。
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import { askRag, testConnection, type Citation, type RagMode } from "@/api/rag";
import { PROVIDERS, findProvider } from "@/api/providers";
import { defaultSettings, formatYuan, isReady, loadSettings, saveSettings, clearSettings, type LlmSettings } from "@/api/settings";
import { createSession, emptyMeta, loadSessions, recentContext, saveSessions, titleFrom, uid, type ChatMessage, type ChatSession } from "@/api/sessions";

const STEPS = [
  { key: "decompose", label: "拆解" },
  { key: "retrieve", label: "检索" },
  { key: "reflect", label: "反思" },
  { key: "generate", label: "生成" },
  { key: "hallucination", label: "校验" },
];
/** 快速切换的目标服务商：免费档（见 api/providers.ts 的 zhipu 预设）。 */
const QUICK_PROVIDER_ID = "zhipu";
/** 「自定义…」在下拉里的哨兵值（真实模型名不会长这样）。 */
const CUSTOM_MODEL = "__custom__";
const sessions = ref<ChatSession[]>([]);
const activeId = ref("");
const draft = ref("");
const busy = ref(false);
const settings = ref<LlmSettings | null>(null);
const showSettings = ref(false);
const form = ref<LlmSettings>(defaultSettings());
const testState = ref<"" | "testing" | "ok" | "fail">("");
const testMsg = ref("");
const copiedId = ref("");
/** 模型字段处于「自定义…」录入态（下拉里选自定义，或服务商没有预设模型）。 */
const modelCustom = ref(false);
const customModelInput = ref<HTMLInputElement | null>(null);
const apiKeyInput = ref<HTMLInputElement | null>(null);
// 当前展开的引用（键=消息id#序号），同时只展开一条，避免长回答被撑爆。
const openCite = ref("");

function citationKey(msgId: string, index: number): string {
  return msgId + "#" + index;
}

function toggleCite(msgId: string, index: number): void {
  const key = citationKey(msgId, index);
  openCite.value = openCite.value === key ? "" : key;
}
let controller: AbortController | null = null;
let stopped = false;
let typing: Promise<void> | null = null;

const active = computed(() => sessions.value.find((s) => s.id === activeId.value) ?? null);
const messages = computed<ChatMessage[]>(() => active.value?.messages ?? []);

onMounted(() => {
  settings.value = loadSettings();
  const saved = loadSessions();
  sessions.value = saved.length ? saved : [createSession()];
  activeId.value = sessions.value[0].id;
});

function persist(): void {
  saveSessions(sessions.value);
}

function newSession(): void {
  const s = createSession();
  sessions.value.unshift(s);
  activeId.value = s.id;
  persist();
}

function switchSession(id: string): void {
  stop();
  activeId.value = id;
}

function renameSession(s: ChatSession): void {
  const name = window.prompt("重命名对话", s.title);
  if (name === null) return;
  const clean = name.trim();
  if (!clean) return;
  s.title = clean.slice(0, 40);
  persist();
}

function removeSession(id: string): void {
  if (!window.confirm("删除这个对话及其全部消息？")) return;
  sessions.value = sessions.value.filter((s) => s.id !== id);
  if (!sessions.value.length) sessions.value = [createSession()];
  if (activeId.value === id) activeId.value = sessions.value[0].id;
  persist();
}

async function copyMessage(msg: ChatMessage): Promise<void> {
  try {
    await navigator.clipboard.writeText(msg.text);
    copiedId.value = msg.id;
    setTimeout(() => { if (copiedId.value === msg.id) copiedId.value = ""; }, 1500);
  } catch {
    /* 非安全上下文下剪贴板不可用，静默忽略 */
  }
}

function modelLabel(): string {
  const s = settings.value;
  if (!isReady(s)) return "未配置模型";
  const p = findProvider((s as LlmSettings).providerId);
  return (p ? p.name : "自定义") + " · " + (s as LlmSettings).model;
}

function modeLabel(mode: string): string {
  if (mode === "direct") return "直连模型";
  if (mode === "live") return "后端推理";
  return "";
}

/** 当前服务商的预设模型（用于「模型」下拉）。 */
const presetModels = computed<string[]>(() => findProvider(form.value.providerId)?.models ?? []);
/** 是否显示自定义模型输入框：点了「自定义…」，或该服务商没有预设模型。 */
const showCustomModel = computed(() => modelCustom.value || presetModels.value.length === 0);
/** 「模型」下拉当前值：自定义录入态或非预设模型时落在哨兵项上。 */
const modelSelectValue = computed(() =>
  showCustomModel.value ? CUSTOM_MODEL : form.value.model,
);

/** 快速切换候选：免费档两个模型。 */
const quickModels = computed<string[]>(() => findProvider(QUICK_PROVIDER_ID)?.models ?? []);
/** 当前已配置的模型名（未配置为空串）。 */
const currentModel = computed(() => settings.value?.model?.trim() ?? "");
/** 直连就绪（有 Key + 地址 + 模型）→ 问答走直连，否则走后端模型。 */
const directReady = computed(() => isReady(settings.value));
/** 模式小字：就绪显示直连的模型名，未就绪提示走后端。 */
const quickModeText = computed(() => (directReady.value ? `直连 ${currentModel.value}` : "后端模型"));
/** 快速切换下拉的可选项：预设 + 当前值（若不在预设里则置顶显示，避免落到错误项）。 */
const quickOptions = computed<string[]>(() => {
  const list = [...quickModels.value];
  if (currentModel.value && !list.includes(currentModel.value)) list.unshift(currentModel.value);
  return list;
});
/** 快速切换下拉当前值：未配置时展示默认档（选中即写入），配的是别家模型时落在哨兵项。 */
const quickSelectValue = computed(() => {
  const cur = currentModel.value;
  if (!cur) return quickModels.value[0] ?? CUSTOM_MODEL;
  return quickOptions.value.includes(cur) ? cur : CUSTOM_MODEL;
});

/**
 * 打开模型设置抽屉。
 *
 * @param opts.customModel true 时直接进入「自定义模型」录入态并聚焦输入框；
 * @param opts.focusKey true 时聚焦 API Key（未配置时的「去设置」入口用）。
 */
function openSettings(opts: { customModel?: boolean; focusKey?: boolean } = {}): void {
  const next = settings.value ? { ...settings.value } : defaultSettings();
  if (!next.providerId) {
    // 未配置过：默认带出免费档预设（baseUrl / 单价内置），用户只需填 API Key
    form.value = next;
    pickProvider(QUICK_PROVIDER_ID);
  } else {
    form.value = next;
  }
  modelCustom.value =
    opts.customModel === true ||
    (!!form.value.model && !(findProvider(form.value.providerId)?.models ?? []).includes(form.value.model));
  testState.value = "";
  testMsg.value = "";
  showSettings.value = true;
  if (opts.customModel) void nextTick(() => customModelInput.value?.focus());
  else if (opts.focusKey) void nextTick(() => apiKeyInput.value?.focus());
}

function pickProvider(id: string): void {
  form.value.providerId = id;
  const p = findProvider(id);
  if (!p) return;
  if (p.baseUrl) form.value.baseUrl = p.baseUrl;
  form.value.model = p.models[0] ?? form.value.model;
  if (typeof p.priceIn === "number") form.value.priceIn = p.priceIn;
  if (typeof p.priceOut === "number") form.value.priceOut = p.priceOut;
  // 换服务商一律回到下拉态：预设模型已带出，除非该服务商没有预设（如「自定义」）
  modelCustom.value = p.models.length === 0;
  testState.value = "";
}

/**
 * 「模型」下拉切换。
 *
 * @param event change 事件（取值见 modelSelectValue）。
 */
function onModelSelect(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  testState.value = "";
  if (value === CUSTOM_MODEL) {
    modelCustom.value = true;
    void nextTick(() => customModelInput.value?.focus());
    return;
  }
  modelCustom.value = false;
  form.value.model = value;
}

/**
 * 快速切换模型（写入即生效，无需刷新页面）。
 *
 * 只改 model；服务商不是免费档预设时一并把 providerId / baseUrl / 单价切到该预设，
 * 否则会出现「DeepSeek 地址 + GLM 模型名」这种必然失败的组合。API Key 一律不动。
 *
 * @param name 目标模型名。
 */
function applyQuickModel(name: string): void {
  const base = settings.value ?? defaultSettings();
  const next: LlmSettings = { ...base, model: name };
  if (base.providerId !== QUICK_PROVIDER_ID) {
    const p = findProvider(QUICK_PROVIDER_ID);
    next.providerId = QUICK_PROVIDER_ID;
    if (p) {
      next.baseUrl = p.baseUrl;
      if (typeof p.priceIn === "number") next.priceIn = p.priceIn;
      if (typeof p.priceOut === "number") next.priceOut = p.priceOut;
    }
  }
  settings.value = next;
  saveSettings(next);
}

/**
 * 输入区快速切换器的 change 处理：选「自定义（去设置）」时打开抽屉。
 *
 * @param event change 事件。
 */
function onQuickModel(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  if (value === CUSTOM_MODEL) {
    openSettings({ customModel: true });
    return;
  }
  applyQuickModel(value);
}

async function runTest(): Promise<void> {
  testState.value = "testing";
  testMsg.value = "正在请求…";
  const result = await testConnection(form.value);
  testState.value = result.ok ? "ok" : "fail";
  testMsg.value = result.message;
}

function applySettings(): void {
  const value = { ...form.value };
  settings.value = value;
  saveSettings(value);
  showSettings.value = false;
}

function resetSettings(): void {
  clearSettings();
  settings.value = null;
  form.value = defaultSettings();
  modelCustom.value = false;
  showSettings.value = false;
}

async function scrollToEnd(): Promise<void> {
  await nextTick();
  const main = document.querySelector("main");
  if (main) main.scrollTop = main.scrollHeight;
}

function render(text: string): string {
  const esc = (s: string) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const bold = (s: string) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  return text
    .split(/\n{2,}/)
    .map((para) => {
      const lines = para.split("\n");
      if (lines.every((l) => l.startsWith("- "))) {
        return "<ul>" + lines.map((l) => "<li>" + bold(l.slice(2)) + "</li>").join("") + "</ul>";
      }
      return "<p>" + lines.map(bold).join("<br>") + "</p>";
    })
    .join("");
}

async function typeOut(msg: ChatMessage, full: string, signal?: AbortSignal): Promise<void> {
  const STEP = 6;
  for (let i = 0; i < full.length; i += STEP) {
    // 用户点了「停止」：保留已经打出来的部分就停下。原先这里写的是
    // msg.text = full —— 等于「停止」反而把剩下的正文一次性倒了出来。
    if (signal?.aborted) return;
    msg.text = full.slice(0, i + STEP);
    await new Promise((resolve) => setTimeout(resolve, 12));
  }
  msg.text = full;
}

function stepState(msg: ChatMessage, key: string): string {
  if (msg.state === "done") return "done";
  const meta = msg.meta;
  if (key === "decompose") return meta.subQuestions.length ? "done" : "run";
  if (key === "retrieve") return meta.status || meta.subQuestions.length ? "done" : "run";
  if (key === "reflect") return meta.status ? "done" : "";
  if (key === "generate") return msg.text ? "run" : "";
  if (key === "hallucination") return meta.elapsedMs ? "done" : msg.text ? "run" : "";
  return "";
}

function stop(): void {
  if (!controller) return;
  stopped = true;
  controller.abort();
  busy.value = false;
}

function clearAll(): void {
  stop();
  if (active.value) active.value.messages = [];
  persist();
}

async function run(question: string): Promise<void> {
  const session = active.value;
  if (!session) return;
  const history = recentContext(session.messages);
  const userMsg: ChatMessage = {
    id: uid(), role: "user", text: question, citations: [], state: "done", error: "", meta: emptyMeta(), createdAt: Date.now(),
  };
  // reply 必须是响应式对象：push 一个普通对象之后再改它的属性不会触发重新渲染，
  // 之前只是靠别的状态顺带刷新；点「停止」时恰好没有别的更新，界面就停在
  // 「正在检索」不动，而数据其实早就改好了（localStorage 里 stopped=true）。
  const reply = reactive<ChatMessage>({
    id: uid(), role: "assistant", text: "", citations: [], state: "streaming", error: "", meta: emptyMeta(), createdAt: Date.now(),
  });
  session.messages.push(userMsg, reply);
  if (session.messages.filter((m) => m.role === "user").length === 1) session.title = titleFrom(question);
  session.updatedAt = Date.now();
  busy.value = true;
  controller = new AbortController();
  // 抓住本轮自己的 controller：停止后用户可以立刻发下一条，
  // 若收尾时去操作共享变量，就会把新一轮的状态一起清掉。
  const ctrl = controller;
  stopped = false;
  typing = null;
  await scrollToEnd();

  await askRag(
    question,
    {
      onSubQuestions: (items) => { reply.meta.subQuestions = items; },
      onRetrieve: (info) => { reply.meta.docCount = info.docCount; reply.meta.topScore = info.topScore; },
      onReflect: (info) => { reply.meta.sufficient = info.sufficient; reply.meta.missing = info.missing; },
      onAnswer: (text, isStream) => {
        if (isStream) reply.text = text;
        else typing = typeOut(reply, text, ctrl.signal);
      },
      onHallucination: (info) => {
        reply.meta.hasHallucination = info.hasHallucination;
        reply.meta.sentences = info.sentences;
      },
      onUsage: (info) => {
        reply.meta.promptTokens = info.promptTokens;
        reply.meta.completionTokens = info.completionTokens;
        reply.meta.cost = info.cost;
        reply.meta.costEstimated = info.estimated;
      },
      onCitations: (items) => { reply.citations = items; },
      onStatus: (text) => { reply.meta.status = text; },
      onDone: (info) => {
        reply.meta.elapsedMs = info.elapsedMs;
        reply.meta.traceId = info.traceId;
        reply.meta.mode = info.mode;
      },
      onError: (message) => { reply.state = "error"; reply.error = message; },
    },
    { signal: ctrl.signal, direct: settings.value, history },
  );

  if (typing) await typing;
  if (reply.state === "streaming") {
    reply.state = "done";
    // 中止也要落到消息本身上：否则气泡会一直转圈，重载后这条消息还会因为
    // 「未完成」被排除在多轮上下文之外——用户以为有的上下文其实已经断了。
    if (ctrl.signal.aborted) reply.meta.stopped = true;
  }
  if (controller === ctrl) {
    busy.value = false;
    controller = null;
  }
  persist();
  await scrollToEnd();
}

async function send(preset?: string): Promise<void> {
  if (busy.value) { stop(); return; }
  const question = (preset ?? draft.value).trim();
  if (!question) return;
  draft.value = "";
  await run(question);
}

/** 重新生成：丢弃该条助手回答，用其上一条用户提问重跑。 */
async function regenerate(msg: ChatMessage): Promise<void> {
  const session = active.value;
  if (!session || busy.value) return;
  const idx = session.messages.findIndex((m) => m.id === msg.id);
  if (idx < 1) return;
  const prev = session.messages[idx - 1];
  if (prev.role !== "user") return;
  session.messages.splice(idx, 1);
  const question = prev.text;
  session.messages.splice(idx - 1, 1);
  await run(question);
}

function onKey(event: KeyboardEvent): void {
  if (event.key !== "Enter" || event.shiftKey) return;
  event.preventDefault();
  void send();
}
</script>
<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">Module / Chat</div>
        <h2>智能问答</h2>
        <p>答案可溯源到知识库条目</p>
      </div>
      <div class="ph-actions">
        <span class="model-tag" :class="{ on: isReady(settings) }">{{ modelLabel() }}</span>
        <button v-if="messages.length" class="btn" @click="clearAll()">清空</button>
        <button class="btn" @click="openSettings()">设置</button>
      </div>
    </div>

    <div class="chat-layout">
      <aside class="sessions">
        <button class="new-chat" @click="newSession()">＋ 新对话</button>
        <div class="session-list">
          <div
            v-for="s in sessions"
            :key="s.id"
            class="session-item"
            :class="{ active: s.id === activeId }"
            @click="switchSession(s.id)"
          >
            <span class="s-title" :title="s.title" @dblclick.stop="renameSession(s)">{{ s.title }}</span>
            <span class="s-count">{{ s.messages.filter((m) => m.role === 'user').length }}</span>
            <button class="s-del" title="删除对话" @click.stop="removeSession(s.id)">✕</button>
          </div>
        </div>
        <div class="sessions-hint">双击标题可重命名</div>
      </aside>

      <div class="chat">
        <div v-if="!messages.length" class="empty">
          <div class="empty-hint">输入问题开始问答。</div>
        </div>

        <div v-for="msg in messages" :key="msg.id" class="msg" :class="msg.role">
          <div v-if="msg.role === 'user'" class="u-text">{{ msg.text }}</div>
          <div v-else class="a-body">
            <div v-if="settings?.showSteps !== false" class="route">
              <span v-for="step in STEPS" :key="step.key" class="rstep" :class="stepState(msg, step.key)">
                <i></i>{{ step.label }}
              </span>
            </div>
            <div v-if="msg.meta.subQuestions.length" class="chips">
              <span v-for="q in msg.meta.subQuestions" :key="q" class="chip">{{ q }}</span>
            </div>
            <div v-if="msg.text" class="answer" v-html="render(msg.text)"></div>
            <div v-else-if="msg.state === 'streaming'" class="wait">正在检索知识库…</div>
            <div v-if="msg.citations.length && settings?.showCitations !== false" class="cites">
              <div class="cites-head">引用来源</div>
              <template v-for="(c, i) in msg.citations" :key="(c.chunk_id || c.id || i) + '-' + i">
                <div class="cite" :class="{ open: openCite === citationKey(msg.id, i) }" @click="toggleCite(msg.id, i)">
                  <span class="ci">{{ i + 1 }}</span>
                  <span class="ct">{{ c.filename || c.title || "未命名来源" }}</span>
                  <span class="cs">{{ c.score.toFixed(4) }}</span>
                  <span class="cx">{{ openCite === citationKey(msg.id, i) ? "收起" : "查看" }}</span>
                </div>
                <div v-if="openCite === citationKey(msg.id, i)" class="cite-body">
                  <div v-if="c.locatable" class="range mono">原文第 {{ c.char_start }}–{{ c.char_end }} 字符<span v-if="c.chunk_index !== undefined"> · 分段 #{{ c.chunk_index }}</span></div>
                  <div v-else class="range warn">该来源无法定位到原文位置（{{ c.degrade_reason || "no_text_coordinate" }}）</div>
                  <div class="cite-text">{{ c.content || c.snippet || "（无正文）" }}</div>
                </div>
              </template>
            </div>
            <div v-if="msg.state === 'error'" class="err">{{ msg.error }}</div>
            <div v-if="msg.state === 'done' && (msg.meta.mode || msg.meta.stopped)" class="meta">
              <span>{{ msg.meta.status || modeLabel(msg.meta.mode) }}</span>
              <span v-if="msg.meta.docCount">{{ msg.meta.docCount }} 篇候选 · 最高分 {{ msg.meta.topScore.toFixed(4) }}</span>
              <span v-if="msg.meta.elapsedMs">{{ msg.meta.elapsedMs }} ms</span>
              <span v-if="msg.meta.promptTokens">输入 {{ msg.meta.promptTokens }} / 输出 {{ msg.meta.completionTokens }} tokens</span>
              <span v-if="msg.meta.cost > 0">本次 {{ formatYuan(msg.meta.cost) }}<template v-if="msg.meta.costEstimated">（估算）</template></span>
              <span v-if="msg.meta.stopped" class="warn">已停止生成</span>
              <span v-else-if="msg.meta.hasHallucination" class="warn">检出疑似幻觉 {{ msg.meta.sentences.length }} 处</span>
              <span v-else-if="msg.meta.elapsedMs" class="ok">幻觉校验通过</span>
              <span v-if="msg.meta.traceId" class="mono">{{ msg.meta.traceId }}</span>
            </div>
            <div v-if="msg.state !== 'streaming'" class="acts">
              <button class="act" @click="copyMessage(msg)">{{ copiedId === msg.id ? "已复制" : "复制" }}</button>
              <button class="act" :disabled="busy" @click="regenerate(msg)">重新生成</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="composer">
      <div class="composer-bar">
        <label class="cb-lab" for="quick-model">模型</label>
        <select
          id="quick-model"
          class="cb-sel"
          :class="{ on: directReady }"
          :value="quickSelectValue"
          @change="onQuickModel"
        >
          <option v-for="m in quickOptions" :key="m" :value="m">{{ m }}</option>
          <option :value="CUSTOM_MODEL">自定义（去设置）</option>
        </select>
        <span class="cb-mode" :class="{ on: directReady }">{{ quickModeText }}</span>
        <button
          v-if="!directReady"
          class="cb-setup"
          type="button"
          @click="openSettings({ focusKey: true })"
        >
          填入 API Key 后生效 · 去设置
        </button>
      </div>
      <div class="composer-row">
        <textarea
          v-model="draft"
          rows="1"
          placeholder="描述设备现象或直接提问 · Enter 发送 / Shift+Enter 换行"
          @keydown="onKey"
        ></textarea>
        <button class="btn" :class="busy ? 'btn-mo' : 'btn-zhu'" :disabled="!busy && !draft.trim()" @click="send()">
          {{ busy ? "停止" : "发送" }}
        </button>
      </div>
    </div>

    <div v-if="showSettings" class="mask" @click.self="showSettings = false">
      <div class="modal">
        <div class="modal-head">
          <h3>模型设置</h3>
          <button class="x" @click="showSettings = false">✕</button>
        </div>
        <div class="modal-body">
          <div class="sect">模型接入</div>
          <div class="field">
            <label>服务商</label>
            <select :value="form.providerId" @change="pickProvider(($event.target as HTMLSelectElement).value)">
              <option v-for="p in PROVIDERS" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
            <div v-if="findProvider(form.providerId)?.note" class="hint">{{ findProvider(form.providerId)?.note }}</div>
          </div>
          <div class="field">
            <label>API Key</label>
            <div class="row">
              <input
                ref="apiKeyInput"
                v-model="form.apiKey"
                type="password"
                autocomplete="off"
                placeholder="粘贴服务商控制台里的密钥"
              />
              <button class="btn" :disabled="testState === 'testing'" @click="runTest()">
                {{ testState === "testing" ? "测试中" : "测试连接" }}
              </button>
            </div>
            <div v-if="testMsg" class="hint" :class="testState">{{ testMsg }}</div>
            <div v-else-if="!form.apiKey.trim() && form.providerId !== 'ollama'" class="hint warn">
              填入 API Key 后生效（本页其余设置会先保存，直连等 Key 就位后自动启用）。
            </div>
            <div v-else class="hint">仅保存在本机浏览器，不会发往所选服务商以外的地址。</div>
          </div>
          <div class="field">
            <label for="chat-model">模型</label>
            <select id="chat-model" :value="modelSelectValue" @change="onModelSelect">
              <option v-for="m in presetModels" :key="m" :value="m">{{ m }}</option>
              <option :value="CUSTOM_MODEL">自定义…</option>
            </select>
            <input
              v-if="showCustomModel"
              ref="customModelInput"
              v-model="form.model"
              class="custom-model"
              placeholder="输入模型名（OpenAI 兼容服务）"
            />
            <div v-if="showCustomModel" class="hint">模型名以服务商文档为准；免费档可直接用上面的预设。</div>
          </div>
          <div class="field">
            <label>接口地址</label>
            <input v-model="form.baseUrl" placeholder="https://…/v1" />
            <div class="hint">需为 OpenAI 兼容地址；开发环境会自动经本机代理转发以绕过浏览器跨域限制。</div>
          </div>
          <div class="sect">生成参数</div>
          <div class="grid-fields">
            <div class="field">
              <label>温度</label>
              <input v-model.number="form.temperature" type="number" min="0" max="2" step="0.1" />
              <div class="hint">越低越稳定，诊断类建议 0 ~ 0.3</div>
            </div>
            <div class="field">
              <label>最大输出 tokens</label>
              <input v-model.number="form.maxTokens" type="number" min="128" max="32768" step="128" />
              <div class="hint">回答长度上限</div>
            </div>
          </div>
          <div class="sect">检索与显示</div>
          <div class="grid-fields">
            <div class="field">
              <label>检索条数</label>
              <input v-model.number="form.topK" type="number" min="1" max="10" step="1" />
              <div class="hint">送入模型的资料条数</div>
            </div>
            <div class="field" style="padding-top: 22px">
              <label class="check"><input v-model="form.useRag" type="checkbox" />先检索知识库再提问（需后端可用）</label>
              <label class="check"><input v-model="form.showCitations" type="checkbox" />显示引用来源</label>
              <label class="check"><input v-model="form.showSteps" type="checkbox" />显示链路步骤</label>
            </div>
          </div>
          <div class="sect">计费（元 / 百万 tokens）</div>
          <div class="grid-fields">
            <div class="field">
              <label>输入单价</label>
              <input v-model.number="form.priceIn" type="number" min="0" step="0.1" />
            </div>
            <div class="field">
              <label>输出单价</label>
              <input v-model.number="form.priceOut" type="number" min="0" step="0.1" />
            </div>
          </div>
          <div class="hint" style="margin-bottom: 14px">选服务商时会带出默认单价，按实际合同价修改即可；回答下方会显示每次调用的折算成本。</div>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="resetSettings()">清除配置</button>
          <div class="foot-right">
            <button class="btn" @click="showSettings = false">取消</button>
            <button class="btn btn-zhu" @click="applySettings()">保存并启用</button>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.chat-layout{display:flex;gap:20px;align-items:flex-start}
.sessions{width:208px;flex-shrink:0;position:sticky;top:0}
.new-chat{width:100%;padding:8px 10px;font-family:var(--f-hei);font-size:12px;color:var(--mo-2);background:var(--zhi-1);border:1px solid var(--bian-2);border-radius:var(--r-1);cursor:pointer;transition:border-color .15s,color .15s}
.new-chat:hover{border-color:var(--zhu-1);color:var(--zhu-1)}
.session-list{margin-top:8px;max-height:52vh;overflow-y:auto}
.session-item{display:flex;align-items:center;gap:6px;padding:7px 9px;border-radius:var(--r-1);cursor:pointer;font-size:12px;color:var(--mo-2);transition:background-color .15s}
.session-item:hover{background:var(--zhi-3)}
.session-item.active{background:rgba(var(--brand-rgb),.08);color:var(--mo-1)}
.s-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.s-count{font-family:var(--f-mono);font-size:10px;color:var(--mo-4)}
.s-del{border:none;background:none;color:var(--mo-5);font-size:11px;cursor:pointer;padding:0 2px;opacity:0}
.session-item:hover .s-del{opacity:1}
.s-del:hover{color:var(--st-danger-text)}
.sessions-hint{margin-top:8px;font-size:10px;color:var(--mo-4);line-height:1.6}
.chat{flex:1;min-width:0;max-width:860px}
.model-tag{font-size:10.5px;color:var(--mo-4);border:1px solid var(--bian-1);border-radius:var(--r-1);padding:5px 9px;font-family:var(--f-mono);letter-spacing:0;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.model-tag.on{color:var(--song-1);border-color:rgba(62,92,74,.3);background:rgba(62,92,74,.06)}
.empty{padding:40px 0 0}
.empty-hint{font-size:12px;color:var(--mo-3);line-height:1.8;margin-bottom:18px;max-width:520px}
.suggest{display:flex;align-items:center;gap:9px;width:100%;text-align:left;padding:11px 14px;margin-bottom:7px;cursor:pointer;background:var(--zhi-card);border:1px solid var(--bian-1);border-radius:var(--r-1);font-family:var(--f-hei);font-size:12.5px;color:var(--mo-2);transition:border-color .15s,color .15s}
.suggest:hover{border-color:var(--bian-3);color:var(--mo-1)}
.s-arrow{color:var(--zhu-1);font-size:11px}
.msg{margin-bottom:26px}
.msg.user{display:flex;justify-content:flex-end}
.u-text{max-width:78%;background:var(--zhi-4);border:1px solid var(--bian-1);border-radius:var(--r-1);padding:9px 13px;font-size:12.5px;color:var(--mo-1);line-height:1.7}
.a-body{border-left:2px solid var(--zhu-1);padding-left:16px}
.route{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:11px}
.rstep{display:inline-flex;align-items:center;gap:5px;font-size:10.5px;color:var(--mo-4);letter-spacing:.3px}
.rstep i{width:5px;height:5px;border-radius:50%;background:var(--bian-2)}
.rstep.run{color:var(--teng-3)}
.rstep.run i{background:var(--teng-1)}
.rstep.done{color:var(--song-1)}
.rstep.done i{background:var(--song-1)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.chip{font-size:10.5px;color:var(--mo-3);background:var(--zhi-3);border:1px solid var(--bian-1);border-radius:2px;padding:2px 8px}
.answer{font-size:13px;line-height:1.9;color:var(--mo-1)}
.answer :deep(p){margin-bottom:11px}
.answer :deep(ul){margin:0 0 11px 2px;padding-left:15px;list-style:none}
.answer :deep(li){position:relative;margin-bottom:5px;color:var(--mo-2)}
.answer :deep(li)::before{content:'·';position:absolute;left:-11px;color:var(--mo-4)}
.answer :deep(strong){font-weight:600;color:var(--mo-1)}
.wait{font-size:12px;color:var(--mo-4);padding:4px 0}
.cites{margin-top:15px;border-top:1px solid var(--bian-1);padding-top:11px}
.cites-head{font-size:10.5px;color:var(--mo-4);letter-spacing:1px;margin-bottom:7px}
.cite{display:flex;align-items:baseline;gap:9px;padding:5px 7px;margin:1px -7px;border-radius:var(--r-1);font-size:11.5px;cursor:pointer;transition:background-color .15s}
.cite:hover{background:var(--zhi-3)}
.cite.open{background:var(--zhi-4)}
.cx{font-size:10.5px;color:var(--zhu-1);flex-shrink:0}
.cite-body{margin:2px 0 8px;padding:9px 11px;background:var(--zhi-3);border-left:2px solid var(--zhu-1);border-radius:0 var(--r-1) var(--r-1) 0}
.cite-body .range{font-size:10.5px;color:var(--mo-4);margin-bottom:6px}
.cite-body .range.warn{color:var(--teng-3)}
.cite-text{font-size:11.5px;line-height:1.8;color:var(--mo-2);white-space:pre-wrap;word-break:break-word}
.ci{font-family:var(--f-mono);font-size:10px;color:#FFFFFF;background:var(--mo-3);border-radius:2px;padding:1px 5px;flex-shrink:0}
.ct{flex:1;color:var(--mo-2);line-height:1.6}
.cs{font-family:var(--f-mono);font-size:10.5px;color:var(--mo-4);flex-shrink:0}
.meta{display:flex;flex-wrap:wrap;gap:14px;margin-top:13px;font-size:10.5px;color:var(--mo-4)}
.meta .ok{color:var(--song-1)}
.meta .warn{color:var(--st-danger-text)}
.meta .mono{font-family:var(--f-mono)}
.err{margin-top:10px;font-size:11.5px;color:var(--st-danger-text);line-height:1.7}
.acts{display:flex;gap:12px;margin-top:11px}
.act{border:none;background:none;padding:0;font-family:var(--f-hei);font-size:11px;color:var(--mo-4);cursor:pointer}
.act:hover{color:var(--zhu-1)}
.act:disabled{color:var(--mo-5);cursor:not-allowed}
.composer{position:sticky;bottom:0;padding:12px 0 16px;margin-top:8px;background:var(--zhi-2);border-top:1px solid var(--bian-1);max-width:860px;margin-left:228px}
/* 输入区上方的模型条：紧凑一行，左对齐，不抢输入区的视觉重心 */
.composer-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.cb-lab{font-size:10.5px;color:var(--mo-4);letter-spacing:.4px}
.cb-sel{height:24px;max-width:190px;padding:0 6px;font-family:var(--f-mono);font-size:11px;color:var(--mo-2);background:var(--zhi-1);border:1px solid var(--bian-2);border-radius:var(--r-1);cursor:pointer;outline:none;transition:border-color .15s,color .15s}
.cb-sel:hover{border-color:var(--bian-3)}
.cb-sel:focus{border-color:var(--zhu-1)}
.cb-sel.on{color:var(--song-1);border-color:rgba(62,92,74,.3);background:rgba(62,92,74,.06)}
.cb-mode{font-family:var(--f-mono);font-size:10.5px;color:var(--mo-4);letter-spacing:0}
.cb-mode.on{color:var(--song-1)}
.cb-setup{padding:0;border:none;border-bottom:1px dashed transparent;background:none;font-family:var(--f-hei);font-size:10.5px;color:var(--zhu-1);cursor:pointer}
.cb-setup:hover{border-bottom-color:currentColor}
.composer-row{display:flex;gap:9px;align-items:flex-end}
.composer textarea{flex:1;resize:none;min-height:36px;max-height:132px;padding:9px 12px;font-family:var(--f-hei);font-size:12.5px;line-height:1.6;color:var(--mo-1);background:var(--zhi-1);border:1px solid var(--bian-2);border-radius:var(--r-1);outline:none;transition:border-color .15s}
.composer textarea:focus{border-color:var(--zhu-1)}
.composer textarea::placeholder{color:var(--mo-5)}
.composer .btn{height:36px;flex-shrink:0}
.composer .btn:disabled{opacity:.45}
.mask{position:fixed;inset:0;background:rgba(20,20,20,.42);display:flex;align-items:center;justify-content:center;z-index:60}
.modal{width:560px;max-width:calc(100vw - 40px);max-height:calc(100vh - 60px);overflow-y:auto;background:var(--zhi-1);border:1px solid var(--bian-2);border-radius:var(--r-2);box-shadow:0 16px 48px -16px rgba(0,0,0,.45)}
.modal-head{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--bian-1)}
.modal-head h3{font-family:var(--f-song);font-size:15px;font-weight:600;color:var(--mo-1);letter-spacing:.5px}
.x{border:none;background:none;font-size:13px;color:var(--mo-4);cursor:pointer;padding:4px 6px}
.x:hover{color:var(--mo-1)}
.modal-body{padding:16px 20px 4px}
.sect{font-size:10.5px;color:var(--mo-4);letter-spacing:1.2px;font-weight:600;margin:6px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--bian-1)}
.field{margin-bottom:14px}
.field label{display:block;font-size:11px;color:var(--mo-3);letter-spacing:.4px;margin-bottom:6px}
.field input,.field select{width:100%;padding:8px 11px;font-family:var(--f-hei);font-size:12.5px;color:var(--mo-1);background:var(--zhi-1);border:1px solid var(--bian-2);border-radius:var(--r-1);outline:none;transition:border-color .15s}
.field input:focus,.field select:focus{border-color:var(--zhu-1)}
.field .hint{font-size:10.5px;color:var(--mo-4);margin-top:5px;line-height:1.65}
.field .hint.ok{color:var(--song-1)}
.field .hint.fail{color:var(--st-danger-text)}
.field .hint.warn{color:var(--st-warning-text)}
.field .custom-model{margin-top:6px}
.row{display:flex;gap:8px}
.row .btn{flex-shrink:0;white-space:nowrap}
.grid-fields{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
.check{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--mo-2);margin-bottom:7px;letter-spacing:0}
.check input{width:auto;accent-color:var(--zhu-1)}
.modal-foot{display:flex;justify-content:space-between;align-items:center;gap:9px;padding:14px 20px;border-top:1px solid var(--bian-1);position:sticky;bottom:0;background:var(--zhi-1)}
.foot-right{display:flex;gap:9px}
@media (max-width:1080px){.sessions{display:none}.composer{margin-left:0}}
</style>