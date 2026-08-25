<script setup>
import { onActivated, ref } from 'vue'
import { useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { startRegister } from '@/api/register'
import { useFormStore, proxyText } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import LogPanel from '@/components/LogPanel.vue'

const route = useRoute()
const { form } = storeToRefs(useFormStore())
const { list: proxyList } = storeToRefs(useProxyStore())
const runtime = useRuntimeStore()
const { runningSingle, lastRunResult } = storeToRefs(runtime)

const starting = ref(false)
const regEmail = ref('')
// 2FA 默认开（主人要求每个号都绑）。绑定不可逆，所以留开关。
// 放在 form store（localStorage 持久化）而不是组件局部 ref —— 组件是
// keep-alive 的，切页不丢，但刷新页面会重建，关了就白关。

// 从「邮箱列表 → 使用」跳转过来时，带上指定邮箱
onActivated(() => {
  if (route.query.email) regEmail.value = String(route.query.email)
})

async function run() {
  starting.value = true
  runtime.clearLogs()
  lastRunResult.value = null
  try {
    const r = await startRegister({
      email: regEmail.value.trim() || null,
      proxy: proxyText(form.value),
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 10,
      want_access_token: true,
      want_session_token: true,
      want_refresh_token: true,
      want_2fa: form.value.want2fa,
    })
    runtime.addLog(`[client] 启动注册 run_id=${r.run_id} email=${r.email}`, 'evt')
    runtime.streamRun(r.run_id)
  } catch (e) {
    ElMessage.error(e.message)
    lastRunResult.value = { error: e.message }
  } finally {
    starting.value = false
  }
}

</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">单次注册</span></template>
          <el-form label-position="top">
            <el-form-item label="邮箱（留空 = 自动 claim 下一个 available）">
              <el-input v-model="regEmail" placeholder="留空 = 自动选号 / 或填指定邮箱" clearable />
            </el-form-item>
            <el-form-item label="本次使用的单个代理（可从代理池选，或手动输入；直连留空）">
              <el-select
                v-model="form.proxy" filterable clearable allow-create default-first-option
                :reserve-keyword="false" placeholder="socks5://user:pass@host:1080"
                style="width: 100%"
              >
                <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
              </el-select>
              <div class="hint" style="margin-top: 4px">
                Plus 检测、自动批量的兜底代理都复用这里；批量并发轮换请到「代理池」页管理。
              </div>
            </el-form-item>
            <el-form-item label="OTP 等待秒数">
              <el-input-number v-model="form.otpTimeout" :min="10" :max="600" />
            </el-form-item>
            <el-form-item>
              <div style="display: flex; align-items: center; gap: 10px">
                <el-switch v-model="form.want2fa" />
                <span>注册成功后自动绑定 2FA（TOTP）</span>
              </div>
              <div class="hint" style="margin-top: 6px; line-height: 1.5">
                默认开。绑定不可逆、即刻生效：<b>之后该号所有登录都需 6 位动态码</b>；
                secret 仅在绑定时下发<b>一次</b>、服务端取不回，
                请在下方结果或「注册结果」页<b>立刻复制导出</b>并录入验证器，丢失 = 该号 2FA 永久锁死。
                仅对<b>有密码</b>的号生效，无密码号会自动跳过。
              </div>
            </el-form-item>
            <el-button type="primary" :loading="starting || runningSingle" @click="run">
              开始注册
            </el-button>
          </el-form>

          <el-alert
            v-if="lastRunResult && !lastRunResult.error"
            type="success" :closable="false" style="margin-top: 14px"
            :title="lastRunResult.partial ? '注册完成（部分凭证）' : '注册完成'"
          >
            <!-- title 只放「注册完成」；凭证走 description 槽，避免和标题挤一行。 -->
            <div class="run-result">
              <div class="cred-line">
                <span class="cred-label">邮箱</span>
                <code class="cred-val">{{ lastRunResult.email || '—' }}</code>
              </div>
              <div class="cred-line">
                <span class="cred-label">密码</span>
                <code v-if="lastRunResult.password" class="cred-val">{{ lastRunResult.password }}</code>
                <span v-else class="hint">未设置</span>
              </div>
              <div class="cred-line">
                <span class="cred-label">2FA</span>
                <code v-if="lastRunResult.totp_secret" class="cred-val">{{ lastRunResult.totp_secret }}</code>
                <span v-else class="hint">未绑定</span>
              </div>
            </div>
          </el-alert>
          <el-alert
            v-else-if="lastRunResult && lastRunResult.error"
            type="error" :closable="false" style="margin-top: 14px" :title="lastRunResult.error"
          />
        </el-card>
      </el-col>

      <el-col :md="14" style="margin-bottom: 16px">
        <el-card shadow="never">
          <LogPanel />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
:deep(.el-alert__content) { width: 100%; }
:deep(.el-alert__description) {
  margin-top: 8px;
  width: 100%;
}

.run-result {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.run-result .cred-line {
  margin-top: 0;
  align-items: flex-start;
}
.run-result .cred-label {
  flex: 0 0 2.5em;
  white-space: nowrap;
  line-height: 22px;
}
/* .el-alert 是 overflow:hidden，值撑出去不会出滚动条、只会被无声截断，
   而 2FA secret 少一个字符就等于号废了。min-width:0 让它能收缩，
   窄窗口下宁可折进自己这行，也不能被剪掉。 */
.run-result .cred-val {
  min-width: 0;
  line-height: 18px;
}
</style>
