<script setup>
import { onActivated, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listRuns, deleteRun, bulkDeleteRuns } from '@/api/register'
import { fmtTime } from '@/api/request'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'

const { dataVersion } = storeToRefs(useRuntimeStore())
const rows = ref([])
const selected = ref([])
const loading = ref(false)

const STATUS_TYPE = { done: 'primary', failed: 'danger', running: 'warning' }

async function load() {
  loading.value = true
  try { const { items } = await listRuns(50); rows.value = items }
  catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

async function confirm(msg, title = '确认') {
  try {
    await ElMessageBox.confirm(msg, title, {
      type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消',
    })
    return true
  } catch {
    return false
  }
}

async function deleteOne(row) {
  if (row.status === 'running') {
    ElMessage.info('进行中的记录不能删')
    return
  }
  if (!(await confirm(`删除这条运行记录？\n${row.run_id}`))) return
  try {
    await deleteRun(row.run_id)
    ElMessage.success('已删除')
    selected.value = []
    load()
  } catch (e) { ElMessage.error(e.message) }
}

async function deleteSelected() {
  const ids = selected.value.filter((r) => r.status !== 'running').map((r) => r.run_id)
  if (!ids.length) {
    ElMessage.info('请先勾选已结束的记录（进行中的不能删）')
    return
  }
  if (!(await confirm(`确定删除选中的 ${ids.length} 条运行记录？`))) return
  try {
    const r = await bulkDeleteRuns({ run_ids: ids })
    ElMessage.success(`已删除 ${r.deleted} 条`)
    selected.value = []
    load()
  } catch (e) { ElMessage.error(e.message) }
}

async function deleteAll() {
  if (!(await confirm('清空全部已结束的运行记录？进行中的会留下。'))) return
  try {
    const r = await bulkDeleteRuns({ all: true })
    ElMessage.success(`已清空 ${r.deleted} 条`)
    selected.value = []
    load()
  } catch (e) { ElMessage.error(e.message) }
}

watch(dataVersion, () => load())
onActivated(() => load())
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap">
          <span class="section-title" style="margin: 0">运行记录</span>
          <el-space wrap>
            <el-button size="small" @click="load"><el-icon><Refresh /></el-icon>刷新</el-button>
            <el-button
              size="small" type="danger" plain
              :disabled="!selected.length"
              @click="deleteSelected"
            >
              删除选中 ({{ selected.length }})
            </el-button>
            <el-button size="small" type="danger" plain @click="deleteAll">清空全部</el-button>
          </el-space>
        </div>
      </template>
      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table
        v-else v-loading="loading" :data="rows" size="small" stripe
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="44" :selectable="(row) => row.status !== 'running'" />
        <el-table-column prop="run_id" label="run_id" width="180">
          <template #default="{ row }"><span class="mono">{{ row.run_id }}</span></template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="200" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <StatusDot :type="STATUS_TYPE[row.status] || 'info'" :text="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="开始时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column prop="error" label="错误" min-width="200" show-overflow-tooltip />
        <el-table-column label="" width="80" align="right">
          <template #default="{ row }">
            <el-button
              size="small" text type="danger"
              :disabled="row.status === 'running'"
              @click="deleteOne(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无运行记录" :image-size="70" />
        </template>
      </el-table>
    </el-card>
  </div>
</template>
