/**
 * 品牌配色（换肤）的唯一状态源。
 *
 * 侧栏切换器与「设置 → 外观」共用本模块：只有一处写入点，
 * 避免两个入口各持一份值而在保存时互相覆盖。
 * 落盘复用 appSettings 的存储键（fishcloud.app.settings.v1），
 * 应用方式为 html[data-brand] + <meta name="theme-color">。
 */
import { ref } from "vue";

import { BRAND_OPTIONS, applyAppearance, loadAppSettings, saveAppSettings, type BrandTheme } from "@/api/appSettings";

/**
 * 读取初始品牌（非法值回落 blue-pink）。
 *
 * @returns 品牌方案。
 */
function resolveInitial(): BrandTheme {
  const brand = loadAppSettings().brand;
  return BRAND_OPTIONS.some((item) => item.value === brand) ? brand : "blue-pink";
}

/** 当前品牌（模块级单例，与 useToast 同样的轻量做法）。 */
const brand = ref<BrandTheme>(resolveInitial());

/** 立即把当前品牌应用到 DOM（供首帧后手动同步用）。 */
export function applyBrand(): void {
  document.documentElement.setAttribute("data-brand", brand.value);
  applyAppearance(loadAppSettings());
}

/**
 * 切换品牌：更新内存态 + 落盘 + 立即生效。
 *
 * @param value 目标品牌。
 */
export function setBrand(value: BrandTheme): void {
  brand.value = value;
  const settings = loadAppSettings();
  settings.brand = value;
  saveAppSettings(settings);
  applyAppearance(settings);
}

/**
 * 组合式取用。
 *
 * @returns 当前品牌与切换函数。
 */
export function useBrand(): { brand: typeof brand; setBrand: (value: BrandTheme) => void } {
  return { brand, setBrand };
}
