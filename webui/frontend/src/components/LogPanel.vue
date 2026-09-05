<script setup>
import { computed, nextTick, onActivated, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRuntimeStore } from '@/stores/runtime'
import { useFormStore } from '@/stores/form'

const props = defineProps({
  source: { type: String, default: 'register' },
})

const runtime = useRuntimeStore()
const { logs, reauthLogs } = storeToRefs(runtime)
const { form } = storeToRefs(useFormStore())
const boxRef = ref(null)

const lines = computed(() => (props.source === 'reauth' ? reauthLogs.value : logs.value))
const title = computed(() => (props.source === 'reauth' ? '重新授权日志' : '实时日志'))
const autoScroll = computed(() => !!form.value.logAutoScroll)

function clear() {
  if (props.source === 'reauth') runtime.clearReauthLogs()
  else runtime.clearLogs()
}

let pinned = true

function isNearBottom() {
  const el = boxRef.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 48
}

function onScroll() {
  pinned = isNearBottom()
}

function scrollToBottom() {
  const el = boxRef.value
  if (!el || !autoScroll.value || !pinned) return
  el.scrollTop = el.scrollHeight
}

function stick() {
  nextTick(() => {
    requestAnimationFrame(scrollToBottom)
  })
}

watch(
  () => lines.value[lines.value.length - 1]?.id ?? 0,
  stick,
)

watch(
  () => form.value.logAutoScroll,
  (on) => {
    if (on) {
      pinned = true
      stick()
    }
  },
)

onMounted(stick)
onActivated(() => {
  if (autoScroll.value) pinned = true
  stick()
})
</script>

<template>
  <div class="log-wrap">
    <div class="log-head">
      <span class="section-title" style="margin: 0">{{ title }}</span>
      <div class="log-actions">
        <el-checkbox v-model="form.logAutoScroll" size="small">自动滚动</el-checkbox>
        <el-button size="small" text @click="clear">清空</el-button>
      </div>
    </div>
    <div ref="boxRef" class="log-box" @scroll.passive="onScroll">
      <div v-for="l in lines" :key="l.id" class="line" :class="l.kind">{{ l.text }}</div>
      <div v-if="!lines.length" class="line" style="color: #8a7">等待日志输出…</div>
    </div>
  </div>
</template>

<style scoped>
.log-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.log-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
</style>
