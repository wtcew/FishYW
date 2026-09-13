<script setup lang="ts">
/**
 * 使用指南（独立教学板块，纯文字）。
 *
 * 定位：平台里唯一讲「怎么用、怎么接、权限怎么算」的地方。
 * 各业务模块内不再出现教学段落或示例数据，需要解释时一律指到这里。
 * 内容只描述真实存在的能力与接口，不含示例数据、演示截图或伪造指标。
 */
import AppIcon from "@/components/AppIcon.vue";

/** 模块用途与典型操作。 */
const MODULES: Array<{ name: string; to: string; purpose: string; steps: string[] }> = [
  {
    name: "运行概览",
    to: "/",
    purpose: "平台实况入口：资产规模、事件处置进度、引擎与存储健康。",
    steps: ["看「待处置事件」是否积压，点标题直接进入该事件", "引擎/存储异常时先处理依赖，再回到业务操作"],
  },
  {
    name: "资源中心",
    to: "/assets",
    purpose: "业务线 → 环境 → 资产的统一登记簿（CMDB），事件的「影响面」由此定位。",
    steps: [
      "管理员先建业务线，再在业务线下建环境",
      "登记资产：填环境、名称、类型、标识（IP/域名/实例名）",
      "三元组「环境 + 类型 + 标识」唯一，重复会返回冲突提示",
      "资产详情页可看关联事件与变更历史（变更历史需管理员权限）",
    ],
  },
  {
    name: "事件中心",
    to: "/events",
    purpose: "外部告警与手工登记的汇聚点，承载状态机流转与 AI 研判。",
    steps: [
      "告警接入：由外部系统按 webhook 约定推送（见下），或手工登记事件",
      "认领：把 open 事件接过来（记录认领人）",
      "研判：触发 AI 研判，等待结论（异步，返回后自动刷新）",
      "恢复：填写处置说明后置为 resolved（必须人工触发）",
      "关闭：归档为终态（P1 严重事件须先恢复）",
    ],
  },
  {
    name: "AIOps",
    to: "/aiops/chat",
    purpose: "问答、检索、评估与链路查询：问答用于现场排查，检索用于验证召回，评估用于衡量效果。",
    steps: [
      "智能问答：配置了模型凭据则直连模型，否则走后端推理",
      "混合检索：只检索不生成，用来核对知识库里到底有没有相关材料",
      "效果评估：在黄金测试集上跑指标，结果由后端产出",
      "Agent 链路：用 trace_id 查一次问答/研判的完整明细",
    ],
  },
  {
    name: "平台",
    to: "/admin",
    purpose: "用户与角色、告警规则、审计日志、系统健康；未交付模块以 Phase 2 标注。",
    steps: [
      "用户：建账号、改角色/启停、重置密码（一次性密码仅显示一次）",
      "告警规则：决定 webhook 告警的严重度与是否自动研判",
      "审计日志：所有写操作留痕（操作人 / 对象 / 来源 IP）",
    ],
  },
];

/** 状态机说明。 */
const STATES: Array<{ from: string; to: string; who: string; note: string }> = [
  { from: "open", to: "acknowledged", who: "operator+", note: "认领，记录认领人" },
  { from: "open", to: "closed", who: "operator+", note: "无效告警可直接关闭" },
  { from: "acknowledged", to: "diagnosing", who: "规则/系统", note: "命中 auto_diagnose 规则的自动研判" },
  { from: "acknowledged", to: "resolved", who: "operator+", note: "必须填写处置说明" },
  { from: "acknowledged", to: "closed", who: "operator+", note: "无处置动作直接归档" },
  { from: "diagnosing", to: "acknowledged", who: "系统", note: "研判超时/失败自动回退，避免卡在中间态" },
  { from: "diagnosing", to: "resolved", who: "operator+", note: "必须填写处置说明" },
  { from: "resolved", to: "closed", who: "operator+", note: "终态归档，closed 不可再流转" },
];

/** 角色能力矩阵。 */
const ROLES: Array<{ code: string; name: string; can: string; cannot: string }> = [
  {
    code: "admin",
    name: "管理员",
    can: "全部读操作；资产/事件的读写；用户与角色管理；告警规则维护；删除资产；审计日志与系统健康",
    cannot: "不能停用自己；不能停用最后一个启用中的管理员",
  },
  {
    code: "operator",
    name: "运维工程师",
    can: "全部读操作；登记与编辑资产；手工登记事件；认领/恢复/关闭事件；触发 AI 研判；追加复盘评论",
    cannot: "不能删除资产；不能维护业务线/环境；不能进入系统管理",
  },
  {
    code: "viewer",
    name: "只读用户",
    can: "查看资产、事件、时间线、研判记录、告警规则与角色列表",
    cannot: "任何写操作（界面上写按钮已隐藏，后端同样拒绝）",
  },
];

/** 常见问题的表现与处置。 */
const FAQ: Array<{ q: string; a: string }> = [
  {
    q: "登录页注册提示「注册暂未开放」",
    a: "平台关闭了自助注册（后端返回 403）。请让管理员在「平台 → 平台管理 → 用户」里创建账号。",
  },
  {
    q: "问答提示需要配置模型",
    a: "未配置模型凭据时问答走后端推理；若后端也未就绪会直接报错。可在「平台 → 设置」选择服务商并填写凭据，改用直连模式。",
  },
  {
    q: "页面显示「无法连接后端服务」",
    a: "后端进程未启动或地址不对。确认服务已监听（默认 127.0.0.1:8000），必要时在「设置」里填写后端地址。",
  },
  {
    q: "AIOps 相关操作报 503",
    a: "引擎组件尚未就绪（模型加载中）或知识库为空。稍等片刻重试；检索无命中时先在「知识库」上传文档。",
  },
  {
    q: "事件无法关闭",
    a: "P1 严重事件在恢复前不允许直接关闭；另外 closed 是终态，关闭后不能再流转。",
  },
  {
    q: "链路明细查不到",
    a: "trace 明细保存在服务进程内存中，仅保留最近 200 条，服务重启后清空；请用最近一次问答/研判返回的 trace_id 查询。",
  },
];
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">FishCloud / 平台 / 使用指南</div>
        <h2>使用指南</h2>
        <p>平台定位、模块用途、事件状态机、权限矩阵、告警接入与常见问题。本页是平台内唯一的教学章节。</p>
      </div>
    </div>

    <div class="card card--flat">
      <div class="card-head">
        <div>
          <h3>平台定位</h3>
        </div>
      </div>
      <p class="hp">
        FishCloud 面向真实运维现场：把「资产登记 → 告警接入 → 事件认领 → AI 研判 → 处置恢复 → 归档复盘」串成一条
        可审计、可确认的工作流。所有数据都来自平台自身的库表与引擎，界面不提供任何示例数据或演示流程。
      </p>
    </div>

    <div class="card card--flat">
      <div class="card-head">
        <div>
          <h3>模块用途与典型操作</h3>
          <div class="sub">按左侧顺序即日常使用顺序</div>
        </div>
      </div>
      <div v-for="mod in MODULES" :key="mod.name" class="hp-mod">
        <div class="hpm-head">
          <RouterLink :to="mod.to" class="link link--strong">{{ mod.name }}</RouterLink>
          <span class="hp-purpose">{{ mod.purpose }}</span>
        </div>
        <ol class="hp-steps">
          <li v-for="step in mod.steps" :key="step">{{ step }}</li>
        </ol>
      </div>
    </div>

    <div class="card card--flat">
      <div class="card-head">
        <div>
          <h3>事件状态机</h3>
          <div class="sub">除系统自动流转外，全部由人工触发；resolved 只能人工置位</div>
        </div>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <caption class="sr-only">事件状态流转规则</caption>
          <thead>
            <tr>
              <th scope="col">当前状态</th>
              <th scope="col">可流转到</th>
              <th scope="col">触发方</th>
              <th scope="col">说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in STATES" :key="index">
              <td class="mono">{{ row.from }}</td>
              <td class="mono">{{ row.to }}</td>
              <td class="t-dim">{{ row.who }}</td>
              <td class="t-dim">{{ row.note }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="hp-note">
        非法流转会被后端拒绝（409），界面上的按钮也会按当前状态禁用；
        AI 研判只产出结论与证据，不会替人做恢复或关闭。
      </p>
    </div>

    <div class="card card--flat">
      <div class="card-head">
        <div>
          <h3>角色与权限</h3>
          <div class="sub">权限判定的权威在后端，界面隐藏按钮只是减少误操作</div>
        </div>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <caption class="sr-only">角色能力矩阵</caption>
          <thead>
            <tr>
              <th scope="col">角色</th>
              <th scope="col">可以做</th>
              <th scope="col">限制</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="role in ROLES" :key="role.code">
              <td>
                <span class="cell-main">{{ role.name }}</span>
                <div class="cell-sub">{{ role.code }}</div>
              </td>
              <td class="t-dim">{{ role.can }}</td>
              <td class="t-dim">{{ role.cannot }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card card--flat">
      <div class="card-head">
        <div>
          <h3>告警接入（webhook）</h3>
          <div class="sub">外部系统推送 → 规则匹配 → 生成事件</div>
        </div>
      </div>
      <ol class="hp-steps">
        <li>服务端配置令牌：环境变量 <code>WEBHOOK_TOKENS</code>，格式 <code>来源名:令牌</code>，多个用逗号分隔</li>
        <li>外部系统 POST 到 <code>/api/v1/events/webhook/&lt;source&gt;</code>，请求头带 <code>X-Webhook-Token</code></li>
        <li>请求体可选字段：<code>title</code>、<code>severity</code>（critical/major/minor/info）、<code>asset_identifier</code>、<code>payload</code></li>
        <li>命中告警规则时按规则严重度入库（未命中用请求体给的严重度）；规则若开启自动研判，则同时排队一次 AI 研判</li>
        <li><code>asset_identifier</code> 与资产的「标识」一致时会自动关联资产与环境，研判会带上该资产上下文</li>
      </ol>
      <pre class="hp-code">curl -X POST "http://127.0.0.1:8000/api/v1/events/webhook/zabbix" \
     -H "Content-Type: application/json" \
     -H "X-Webhook-Token: &lt;令牌&gt;" \
     -d '{"title":"磁盘使用率 92%","severity":"major","asset_identifier":"10.20.3.11:3306","payload":{"host":"db-prod-01"}}'</pre>
      <p class="hp-note">返回 202 与事件编号；同一条告警重复推送不会去重，请在规则或上游侧收敛。</p>
    </div>

    <div class="card card--flat">
      <div class="card-head">
        <div>
          <h3>常见问题</h3>
          <div class="sub">界面出现的提示语与对应处置</div>
        </div>
      </div>
      <dl class="hp-faq">
        <template v-for="item in FAQ" :key="item.q">
          <dt>{{ item.q }}</dt>
          <dd>{{ item.a }}</dd>
        </template>
      </dl>
    </div>
  </section>
</template>

<style scoped>
.hp {
  font-size: 12.5px;
  line-height: 1.9;
  color: var(--mo-2);
}
.hp-mod + .hp-mod {
  margin-top: var(--sp-4);
  padding-top: var(--sp-4);
  border-top: 1px solid var(--bd-1);
}
.hpm-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  flex-wrap: wrap;
}
.hp-purpose {
  font-size: 12px;
  color: var(--mo-3);
}
.hp-steps {
  margin-top: 8px;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 12px;
  color: var(--mo-2);
  line-height: 1.75;
}
.hp-steps li::marker {
  color: var(--mo-4);
  font-family: var(--f-mono);
}
.hp-steps code,
.hp-note code {
  font-family: var(--f-mono);
  font-size: 11px;
  padding: 1px 5px;
  border: 1px solid var(--bd-1);
  border-radius: var(--r-1);
  background-color: var(--zhi-3);
  color: var(--mo-1);
  word-break: break-all;
}
.hp-code {
  margin-top: var(--sp-3);
  padding: 11px 13px;
  border: 1px solid var(--bd-1);
  border-radius: var(--r-3);
  background-color: var(--zhi-3);
  font-family: var(--f-mono);
  font-size: 11px;
  line-height: 1.75;
  color: var(--mo-2);
  overflow-x: auto;
  white-space: pre;
}
.hp-note {
  margin-top: var(--sp-3);
  font-size: 11.5px;
  color: var(--mo-4);
  line-height: 1.8;
}
.hp-faq {
  display: grid;
  gap: 4px;
}
.hp-faq dt {
  margin-top: var(--sp-3);
  font-size: 12.5px;
  font-weight: 600;
  color: var(--mo-1);
}
.hp-faq dd {
  font-size: 12px;
  color: var(--mo-3);
  line-height: 1.85;
}
.hp-faq dt:first-of-type {
  margin-top: 0;
}
</style>
