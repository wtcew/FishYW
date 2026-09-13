import { createApp } from "vue";
import { createPinia } from "pinia";

import App from "./App.vue";
import router from "./router";
import { onUnauthorized } from "./api/http";
import { applyAppearance, loadAppSettings } from "./api/appSettings";
import { useAuthStore } from "./stores/auth";
import "./styles/tokens.css";
import "./styles/layout.css";
import "./styles/prototype.css";
import "./styles/platform.css";
// 模块色族应用面（结构化表面的渐变）：必须最后引入 —— 它按 [data-module]
// 覆盖 platform.css 的状态默认值（Tab 激活线 / 表头 / 主按钮等），靠层叠顺序收口。
import "./styles/modules.css";

const app = createApp(App);

// 挂载前先套用外观（明暗主题 + 品牌配色 + 字号/密度/动效），避免首帧闪色。
applyAppearance(loadAppSettings());

// Pinia 必须先于 router 安装：路由守卫会读取 auth store。
app.use(createPinia());
app.use(router);

// 401 统一处理：清登录态 + 跳登录页并记住来路。
// 注册在此处而非 http.ts 内部，避免 api 层反向依赖 router（循环依赖）。
onUnauthorized(() => {
  const auth = useAuthStore();
  auth.reset();
  const current = router.currentRoute.value;
  if (current.name !== "login") {
    void router.replace({ name: "login", query: { redirect: current.fullPath } });
  }
});

app.mount("#app");
