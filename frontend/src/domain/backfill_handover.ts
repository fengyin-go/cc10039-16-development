/** 回填字段交接清单的前端领域层：批量提交与批量复核只服务这一个模块，
 * 通用工作台不感知这些端点，业务差异集中在这里。
 */
import { request } from '@/api/client'

export type HandoverItemInput = {
  change_id: number
  字段定义?: string
  变更版本?: string
  关联字典?: string
  字典版本?: string
}

export type HandoverBatchItemResult = {
  change_id: number
  ok: boolean
  message: string
  entry_id?: number
  湖表名?: string
  变更字段?: string
}

export type HandoverBatchResult = {
  ok: boolean
  message: string
  batch_id: string | null
  created: number
  results: HandoverBatchItemResult[]
}

export type HandoverReviewItemResult = {
  entry_id: number
  批次号: string
  湖表名: string
  变更字段: string
  status: string
  ok: boolean
  message: string
}

export type HandoverReviewResult = {
  ok: boolean
  message: string
  confirmed: number
  failed: number
  closed: number
  results: HandoverReviewItemResult[]
}

/** 值班员一次提交多条已审批的 Schema 变更，生成回填字段交接清单。 */
export async function submitHandoverBatch(items: HandoverItemInput[]): Promise<HandoverBatchResult> {
  const response = await request('/api/cross/handover/batch', {
    method: 'POST',
    body: JSON.stringify({ items }),
  })
  return (await response.json()) as HandoverBatchResult
}

/** 批量复核未交接的交接条目；传批次号时只复核该批次。 */
export async function reviewHandover(batchId?: string): Promise<HandoverReviewResult> {
  const response = await request('/api/cross/handover/review', {
    method: 'POST',
    body: JSON.stringify({ batch_id: batchId ?? null }),
  })
  return (await response.json()) as HandoverReviewResult
}
