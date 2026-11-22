<template>
  <section class="handover-panel" data-testid="handover-batch-panel">
    <header class="handover-head">
      <div>
        <h3>批量交接提交</h3>
        <p class="handover-desc">
          一次提交多条已审批（待审批）的 Schema 变更，逐条生成对应湖表与字段的交接条目；
          复核通过（已确认）后才会发给对应回填任务，已发布的变更不会再次回填。
        </p>
      </div>
    </header>

    <table class="data-table handover-table">
      <thead>
        <tr>
          <th>变更ID</th>
          <th>字段定义（缺省取变更字段）</th>
          <th>关联字典（可选）</th>
          <th>字典版本（可选）</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(item, index) in items" :key="index">
          <td>
            <input
              v-model="item.changeId"
              type="number"
              min="1"
              :data-testid="`handover-change-${index}`"
              placeholder="Schema变更ID"
            />
          </td>
          <td><input v-model="item.fieldDef" :data-testid="`handover-fielddef-${index}`" placeholder="如 amount DECIMAL(18,2)" /></td>
          <td><input v-model="item.dictName" :data-testid="`handover-dict-${index}`" placeholder="字典名称" /></td>
          <td><input v-model="item.dictVersion" :data-testid="`handover-dictver-${index}`" placeholder="版本号" /></td>
          <td>
            <button class="link" type="button" :data-testid="`handover-remove-${index}`" @click="removeItem(index)">
              移除
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div class="page-actions">
      <button class="btn" type="button" data-testid="handover-add" @click="addItem">添加一条</button>
      <button class="btn primary" type="button" data-testid="handover-submit" :disabled="submitting" @click="submit">
        {{ submitting ? '提交中…' : '提交批量交接' }}
      </button>
      <button class="btn" type="button" data-testid="handover-review" :disabled="reviewing" @click="review">
        {{ reviewing ? '复核中…' : '复核未交接条目' }}
      </button>
    </div>

    <ul v-if="results.length" class="handover-results" data-testid="handover-results">
      <li v-for="(result, index) in results" :key="index" :data-ok="String(result.ok)">
        {{ result.ok ? '✓' : '✗' }} {{ result.message }}
      </li>
    </ul>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  reviewHandover,
  submitHandoverBatch,
} from '@/domain/backfill_handover'

const emit = defineEmits<{ (event: 'changed'): void }>()

type HandoverItemRow = {
  changeId: string
  fieldDef: string
  dictName: string
  dictVersion: string
}

const items = ref<HandoverItemRow[]>([{ changeId: '', fieldDef: '', dictName: '', dictVersion: '' }])
const results = ref<{ ok: boolean; message: string }[]>([])
const errorMessage = ref('')
const submitting = ref(false)
const reviewing = ref(false)

function addItem() {
  items.value.push({ changeId: '', fieldDef: '', dictName: '', dictVersion: '' })
}

function removeItem(index: number) {
  items.value.splice(index, 1)
  if (!items.value.length) addItem()
}

async function submit() {
  submitting.value = true
  errorMessage.value = ''
  results.value = []
  try {
    const payload = items.value
      .filter((item) => item.changeId.trim())
      .map((item) => ({
        change_id: Number(item.changeId),
        字段定义: item.fieldDef.trim() || undefined,
        关联字典: item.dictName.trim() || undefined,
        字典版本: item.dictVersion.trim() || undefined,
      }))
    if (!payload.length) throw new Error('请至少填写一条变更ID')
    const result = await submitHandoverBatch(payload)
    results.value = result.results.map((item) => ({
      ok: item.ok,
      message: `变更#${item.change_id}：${item.message}`,
    }))
    if (!result.ok && !result.results.length) throw new Error(result.message)
    emit('changed')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量交接提交失败'
  } finally {
    submitting.value = false
  }
}

async function review() {
  reviewing.value = true
  errorMessage.value = ''
  results.value = []
  try {
    const result = await reviewHandover()
    results.value = result.results.map((item) => ({
      ok: item.ok,
      message: `条目#${item.entry_id} ${item.湖表名} - ${item.变更字段}：${item.message}`,
    }))
    if (!result.results.length) {
      results.value = [{ ok: true, message: result.message }]
    }
    emit('changed')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量复核失败'
  } finally {
    reviewing.value = false
  }
}
</script>

<style scoped>
.handover-panel {
  border: 1px solid var(--line, #d8dee9);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
  background: var(--panel-bg, #fafbfc);
}

.handover-head h3 {
  margin: 0 0 4px;
  font-size: 15px;
}

.handover-desc {
  margin: 0 0 12px;
  color: #5a6472;
  font-size: 13px;
}

.handover-table input {
  width: 100%;
  box-sizing: border-box;
}

.handover-results {
  margin: 12px 0 0;
  padding-left: 18px;
  font-size: 13px;
}

.handover-results li[data-ok='false'] {
  color: #b3261e;
}
</style>
