import { defineStore } from 'pinia'
import { reactive, watch } from 'vue'

const KEY = 'gpt_outlook_register_form_v2'

// 跨页面共享 + localStorage 持久化的表单字段
// （proxy 在 注册 / 自动跑号 / Plus 检测 三处共用）
const defaults = {
  proxy: '',
  otpTimeout: 10,
  autoConcurrency: 1,
  autoCoolDown: 3,
  autoTargetCount: 0,
  // 注册后自动绑 2FA。单次 / 批量都**默认 true**：每个号都要 2FA。
  // 仍然拆成两个字段（而不是共用一个）：单次页是验 bug / 试流程的测试台，
  // 共用的话在那边临时关掉，回头批量跑几百个号就全裸奔了。
  // localStorage 只记住主人上次的选择，不改变默认值：清缓存后两边都回到 true。
  want2fa: true,
  autoWant2fa: true,
  // 注册结果页：自动全量查 Plus（含已检测）。默认关，避免一打开就打 chatgpt.com。
  autoCheckPlus: false,
  // 全量检测游标：刷新后接着上次的号继续，不要从头再扫一遍。
  autoCheckOffset: 0,
  autoCheckLastEmail: '',
  // 上一轮全量检测收工的时间（ms 时间戳，0 = 还没收过轮）。
  // 收工后要满 6 小时才自动开下一轮；放这里是为了刷新页面也不会把冷却清掉。
  autoCheckRoundDoneAt: 0,
  // 实时日志：默认不跟滚，勾上才贴底（单次 / 全自动 / 重新授权共用）。
  logAutoScroll: false,
}

// el-select 的 clearable 清空时把值写成 **undefined**（不是 ''），而 proxy 在三个
// 页面都是 `form.value.proxy.trim()` 直接调 —— 主人点一次叉，下次提交就
// "Cannot read properties of undefined (reading 'trim')"。这里统一兜底成字符串，
// 免得每个调用点各写各的可选链，也顺手挡住 localStorage 里的历史脏值。
export function proxyText(form) {
  return String(form?.proxy ?? '').trim()
}

export const useFormStore = defineStore('form', () => {
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') } catch (_) { saved = {} }
  const form = reactive({ ...defaults, ...saved })

  // clearable 清空后 proxy 会变成 undefined 并被持久化进 localStorage，
  // 刷新页面后依然是 undefined。这里watch 回填成 ''，保证存量数据也是干净的。
  watch(() => form.proxy, (v) => {
    if (v === undefined || v === null) form.proxy = ''
  })

  watch(form, (v) => {
    try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (_) {}
  }, { deep: true })

  return { form }
})
