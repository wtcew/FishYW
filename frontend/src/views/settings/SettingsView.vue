<script setup lang="ts">
// 全局设置：外观、导航、检索、Agent、评估、告警、服务连接与数据管理。
// 采用「改动即生效即持久化」，不设保存按钮，避免用户改完忘记保存。
import { onMounted, ref, watch } from "vue";
import {
  BRAND_OPTIONS,
  METRIC_OPTIONS,
  PAGE_OPTIONS,
  applyAppearance,
  clearAppSettings,
  defaultAppSettings,
  exportAll,
  importAll,
  loadAppSettings,
  saveAppSettings,
  type AppSettings,
  type BrandTheme,
  type Density,
  type ThemeMode,
} from "@/api/appSettings";
import { loadSettings, saveSettings } from "@/api/settings";
import { changePassword } from "@/api/auth";
import { ApiError } from "@/api/http";
import { useBrand } from "@/composables/useBrand";
import { pushToast } from "@/composables/useToast";

const form = ref<AppSettings>(loadAppSettings());
const saved = ref("");
const importError = ref("");

// ── 账户安全：改密（2026-09-13 补 UI 入口；后端与 api 层早已就绪，
//    此前全站没有任何按钮能触达，用户无法自助改密——UI 穷举测试 B-2） ──
const pwdOld = ref("");
const pwdNew = ref("");
const pwdConfirm = ref("");
const pwdError = ref("");
const pwdOk = ref("");
const pwdPending = ref(false);

/**
 * 提交改密：前端先做一致性/长度校验，再调后端（旧密码错误返回 400）。
 */
async function submitPassword(): Promise<void> {
  if (pwdPending.value) return;
  pwdError.value = "";
  pwdOk.value = "";
  if (!pwdOld.value) {
    pwdError.value = "请输入旧密码";
    return;
  }
  if (pwdNew.value.length < 8) {
    pwdError.value = "新密码至少 8 位";
    return;
  }
  if (pwdNew.value !== pwdConfirm.value) {
    pwdError.value = "两次输入的新密码不一致";
    return;
  }
  pwdPending.value = true;
  try {
    await changePassword(pwdOld.value, pwdNew.value);
    pwdOld.value = "";
    pwdNew.value = "";
    pwdConfirm.value = "";
    pwdOk.value = "密码已修改。下次登录请使用新密码。";
    pushToast("密码已修改", "success");
  } catch (error) {
    pwdError.value = error instanceof ApiError ? error.message : "修改失败，请稍后重试";
  } finally {
    pwdPending.value = false;
  }
}

// 品牌是跨页共享状态（侧栏也能切），以 useBrand 为唯一源，
// 避免「设置」保存时用表单里的旧值把侧栏刚切换的配色覆盖回去。
const { brand, setBrand } = useBrand();

/**
 * 切换品牌：写共享状态并同步表单显示。
 *
 * @param value 目标品牌。
 */
function pickBrand(value: BrandTheme): void {
  setBrand(value);
  form.value.brand = value;
}

const THEMES: Array<{ value: ThemeMode; label: string }> = [
  { value: "light", label: "亮色" },
  { value: "dark", label: "暗色" },
  { value: "auto", label: "跟随系统" },
];
const DENSITIES: Array<{ value: Density; label: string }> = [
  { value: "compact", label: "紧凑" },
  { value: "normal", label: "标准" },
  { value: "relaxed", label: "宽松" },
];

onMounted(() => applyAppearance(form.value));

// 侧栏切换品牌后，同步设置页的显示值。
watch(brand, (value) => {
  form.value.brand = value;
});

// 深度侦听：任一字段变化立即生效并落盘（含外观的即时应用）。
watch(
  form,
  (value) => {
    // 品牌以共享状态为准（侧栏与设置页两个入口，避免互相覆盖）。
    value.brand = brand.value;
    saveAppSettings(value);
    applyAppearance(value);
    saved.value = "已保存";
    window.setTimeout(() => { if (saved.value === "已保存") saved.value = ""; }, 1200);
  },
  { deep: true },
);

function resetAll(): void {
  if (!window.confirm("恢复全部全局设置为默认值？此操作不会删除模型设置里的 API Key。")) return;
  form.value = defaultAppSettings();
}

function exportConfig(): void {
  const text = exportAll(form.value, loadSettings());
  const blob = new Blob([text], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "fishcloud-settings-" + new Date().toISOString().slice(0, 10) + ".json";
  a.click();
  URL.revokeObjectURL(url);
}

function pickFile(): void {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json,application/json";
  input.onchange = () => {
    const file = input.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const result = importAll(String(reader.result ?? ""));
      if (!result.ok || !result.app) {
        importError.value = result.error ?? "导入失败";
        return;
      }
      importError.value = "";
      form.value = result.app;
      if (result.llm) saveSettings(result.llm as never);
    };
    reader.readAsText(file);
  };
  input.click();
}

function clearLocal(): void {
  if (!window.confirm("清除本机保存的全部设置与对话历史？此操作不可撤销。")) return;
  clearAppSettings();
  try {
    localStorage.removeItem("fishcloud.chat.sessions.v1");
    localStorage.removeItem("fishcloud.llm.settings.v1");
  } catch {
    /* 隐私模式忽略 */
  }
  form.value = defaultAppSettings();
}

function toggleMetric(name: string): void {
  const list = form.value.metrics;
  const i = list.indexOf(name);
  if (i >= 0) list.splice(i, 1);
  else list.push(name);
}
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">Config / Settings</div>
        <h2>全局设置</h2>
        <p>改动即时生效并保存在本机</p>
      </div>
      <div class="ph-actions">
        <span v-if="saved" class="saved">{{ saved }}</span>
        <button class="btn" @click="resetAll()">恢复默认</button>
      </div>
    </div>

    <div class="wrap">
      <div class="card">
        <div class="card-head"><div><h3>外观</h3><div class="sub">品牌配色、主题、字号、间距密度与动效</div></div></div>
        <div class="row-item">
          <div class="lab">配色</div>
          <div class="seg">
            <button
              v-for="item in BRAND_OPTIONS"
              :key="item.value"
              :class="{ on: form.brand === item.value }"
              @click="pickBrand(item.value)"
            >
              <i class="sw" :style="{ backgroundImage: item.swatch }" aria-hidden="true"></i>{{ item.label }}
            </button>
          </div>
        </div>
        <div class="row-item">
          <div class="lab">主题</div>
          <div class="seg">
            <button v-for="t in THEMES" :key="t.value" :class="{ on: form.theme === t.value }" @click="form.theme = t.value">{{ t.label }}</button>
          </div>
        </div>
        <div class="row-item">
          <div class="lab">字号</div>
          <div class="seg-line">
            <input v-model.number="form.fontScale" type="range" min="0.85" max="1.25" step="0.05" />
            <span class="val">{{ Math.round(form.fontScale * 100) }}%</span>
          </div>
        </div>
        <div class="row-item">
          <div class="lab">密度</div>
          <div class="seg">
            <button v-for="d in DENSITIES" :key="d.value" :class="{ on: form.density === d.value }" @click="form.density = d.value">{{ d.label }}</button>
          </div>
        </div>
        <div class="row-item">
          <div class="lab">减弱动效</div>
          <label class="check"><input v-model="form.reduceMotion" type="checkbox" />关闭过渡与动画</label>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>导航</h3><div class="sub">打开系统时默认停留的页面</div></div></div>
        <div class="row-item">
          <div class="lab">默认页面</div>
          <select v-model="form.defaultPage" class="ctl">
            <option v-for="p in PAGE_OPTIONS" :key="p.value" :value="p.value">{{ p.label }}</option>
          </select>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>检索</h3><div class="sub">召回与融合参数，导出后可作为后端启动配置</div></div></div>
        <div class="grid-fields">
          <div class="field"><label>召回条数 top_k</label><input v-model.number="form.topK" type="number" min="1" max="100" /></div>
          <div class="field"><label>重排保留 top_k_rerank</label><input v-model.number="form.rerankK" type="number" min="1" max="50" /></div>
          <div class="field"><label>RRF 参数 k</label><input v-model.number="form.rrfK" type="number" min="1" max="200" /></div>
          <div class="field"><label>最低相关度</label><input v-model.number="form.minScore" type="number" min="0" max="1" step="0.05" /></div>
        </div>
        <div class="checks">
          <label class="check"><input v-model="form.useVector" type="checkbox" />向量召回</label>
          <label class="check"><input v-model="form.useBm25" type="checkbox" />BM25 召回</label>
          <label class="check"><input v-model="form.useGraph" type="checkbox" />图谱召回</label>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>Agent 行为</h3><div class="sub">反思循环、幻觉重试与语义缓存</div></div></div>
        <div class="grid-fields">
          <div class="field"><label>最大迭代次数</label><input v-model.number="form.maxIterations" type="number" min="1" max="10" /></div>
          <div class="field"><label>幻觉重试上限</label><input v-model.number="form.maxHallucinationRetry" type="number" min="0" max="5" /></div>
          <div class="field"><label>缓存 TTL（秒）</label><input v-model.number="form.cacheTtl" type="number" min="0" max="86400" /></div>
          <div class="field"><label>节点超时（秒）</label><input v-model.number="form.nodeTimeout" type="number" min="5" max="300" /></div>
        </div>
        <div class="checks">
          <label class="check"><input v-model="form.useCache" type="checkbox" />启用语义缓存</label>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>评估</h3><div class="sub">黄金测试集规模与参与计算的指标</div></div></div>
        <div class="row-item">
          <div class="lab">黄金集规模</div>
          <input v-model.number="form.goldenSize" type="number" min="10" max="2000" class="ctl-num" />
        </div>
        <div class="checks">
          <label v-for="m in METRIC_OPTIONS" :key="m" class="check">
            <input type="checkbox" :checked="form.metrics.includes(m)" @change="toggleMetric(m)" />{{ m }}
          </label>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>告警</h3><div class="sub">触发阈值与推送地址</div></div></div>
        <div class="checks">
          <label class="check"><input v-model="form.alertsEnabled" type="checkbox" />启用告警</label>
        </div>
        <div class="field"><label>Webhook 地址</label><input v-model="form.webhookUrl" placeholder="https://…（留空则不推送）" /></div>
        <div class="grid-fields">
          <div class="field"><label>p99 延迟阈值（ms）</label><input v-model.number="form.latencyThreshold" type="number" min="100" max="60000" /></div>
          <div class="field"><label>幻觉率阈值（%）</label><input v-model.number="form.hallucinationThreshold" type="number" min="0" max="100" /></div>
          <div class="field"><label>缓存命中率下限（%）</label><input v-model.number="form.cacheHitThreshold" type="number" min="0" max="100" /></div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>服务连接</h3><div class="sub">后端地址与超时；留空表示走同源 /api</div></div></div>
        <div class="field"><label>后端地址</label><input v-model="form.apiBaseUrl" placeholder="http://127.0.0.1:8000" /></div>
        <div class="grid-fields">
          <div class="field"><label>健康检查间隔（秒，0 为关闭）</label><input v-model.number="form.healthInterval" type="number" min="0" max="3600" /></div>
          <div class="field"><label>请求超时（秒）</label><input v-model.number="form.requestTimeout" type="number" min="5" max="1800" /></div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>账户安全</h3><div class="sub">修改当前账号的登录密码（改密后旧登录凭证到期前仍有效）</div></div></div>
        <div class="field"><label>旧密码</label><input v-model="pwdOld" type="password" autocomplete="current-password" /></div>
        <div class="grid-fields">
          <div class="field"><label>新密码（至少 8 位）</label><input v-model="pwdNew" type="password" autocomplete="new-password" /></div>
          <div class="field"><label>确认新密码</label><input v-model="pwdConfirm" type="password" autocomplete="new-password" /></div>
        </div>
        <div class="acts">
          <button class="btn btn-zhu" :disabled="pwdPending" @click="submitPassword()">
            {{ pwdPending ? "提交中…" : "修改密码" }}
          </button>
        </div>
        <div v-if="pwdError" class="err">{{ pwdError }}</div>
        <div v-if="pwdOk" class="hint">{{ pwdOk }}</div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>数据管理</h3><div class="sub">配置可整体导出、导入或清除</div></div></div>
        <div class="acts">
          <button class="btn" @click="exportConfig()">导出配置</button>
          <button class="btn" @click="pickFile()">导入配置</button>
          <button class="btn btn-danger" @click="clearLocal()">清除本机数据</button>
        </div>
        <div v-if="importError" class="err">{{ importError }}</div>
        <div class="hint">API Key 与对话历史都存在本机浏览器，导出文件含密钥，请勿外传。</div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.wrap{max-width:760px}
.saved{font-size:11px;color:var(--song-1);margin-right:4px}
.row-item{display:flex;align-items:center;gap:14px;padding:9px 0;flex-wrap:wrap}
.row-item .lab{flex:0 0 132px;font-size:12px;color:var(--mo-3)}
.seg{display:flex;border:1px solid var(--bian-2);border-radius:var(--r-1);overflow:hidden}
.seg button{border:none;background:var(--zhi-1);color:var(--mo-3);font-family:var(--f-hei);font-size:12px;padding:6px 14px;cursor:pointer;transition:background-color .15s,color .15s}
.seg button + button{border-left:1px solid var(--bian-2)}
.seg button:hover{color:var(--mo-1)}
.seg button.on{background:var(--zhu-1);color:#FFFFFF}
/* 配色切换按钮里的色块预览（两种方案都要能看清，故加深色描边） */
.seg button .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px;border:1px solid rgba(30,30,26,.25)}
.seg-line{display:flex;align-items:center;gap:12px;flex:1;min-width:220px}
.seg-line input[type=range]{flex:1;accent-color:var(--zhu-1)}
.seg-line .val{font-family:var(--f-mono);font-size:11.5px;color:var(--mo-2);width:44px;text-align:right}
.check{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--mo-2);cursor:pointer}
.check input{accent-color:var(--zhu-1)}
.checks{display:flex;flex-wrap:wrap;gap:18px;padding:6px 0 2px}
.grid-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px 20px;padding:4px 0}
.field{display:flex;flex-direction:column;gap:6px;margin-bottom:10px}
.field label{font-size:11px;color:var(--mo-3)}
.field input,.ctl,.ctl-num{padding:7px 10px;font-family:var(--f-hei);font-size:12.5px;color:var(--mo-1);background:var(--zhi-1);border:1px solid var(--bian-2);border-radius:var(--r-1);outline:none;transition:border-color .15s}
.field input:focus,.ctl:focus,.ctl-num:focus{border-color:var(--zhu-1)}
.ctl{min-width:180px}
.ctl-num{width:120px}
.acts{display:flex;gap:9px;flex-wrap:wrap;padding:4px 0 2px}
.btn-danger{color:var(--st-danger-text);border-color:rgba(var(--danger-rgb),.35)}
.btn-danger:hover{background:rgba(var(--danger-rgb),.07)}
.hint{font-size:10.5px;color:var(--mo-4);margin-top:11px;line-height:1.7}
.err{font-size:11.5px;color:var(--st-danger-text);margin-top:10px}
@media (max-width:900px){.grid-fields{grid-template-columns:1fr}}
</style>