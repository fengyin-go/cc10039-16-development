"""数据回填任务领域服务：管理历史数据回填、补数和断点续做。

支持回填检查点（checkpoint）：中断后可从上次位置继续，不会重复处理已完成的记录。
上游接收 Schema 演进的不兼容变更事件，下游联动治理报告记录回填结果。
启动时读取回填字段交接清单：字段未确认前不能发给回填任务；已确认的字段按
变更版本开始写入，回填记录保留实际使用的字段定义；已发布的变更不会再次回填。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class BackfillJobService(BaseModuleService):
    MODULE = "backfill_job"
    ENTITY = "回填任务"
    OPERATION_TERM = "调度"
    LIST_FIELDS = ["任务名称", "目标湖表", "回填时间范围", "处理记录数", "执行状态"]
    REQUIRED_FIELDS = ["任务名称", "目标湖表", "回填时间范围"]
    STATUS_ORDER = ["待调度", "回填中", "已完成", "回填失败"]
    ACTION_RULES = {"启动回填": "回填中", "取消回填": "回填失败"}
    NEGATIVE_ACTIONS = []

    # 回填检查点：任务 id → 已处理记录偏移
    _checkpoints: dict[int, int] = {}

    def start_backfill(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """启动回填任务：先过交接清单闸门，再检查 Schema 变更状态后启动。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "待调度":
            return None, f"回填任务状态为{entry.get('status')}，不可启动"

        table_name = entry.get("目标湖表", "")
        time_range = entry.get("回填时间范围", "")

        # 检查同湖表是否有执行中的回填
        active_backfills = [
            bj for bj in store.rows(self.MODULE)
            if bj.get("目标湖表") == table_name
            and bj.get("status") == "回填中"
            and bj.get("id") != entry_id
        ]
        if active_backfills:
            return None, f"湖表 {table_name} 已有回填任务在执行中"

        # 交接清单闸门：字段未确认前不能发给对应回填任务
        handover_rows = [
            row for row in store.rows("backfill_handover")
            if row.get("湖表名") == table_name
        ]
        unconfirmed = [
            row for row in handover_rows
            if row.get("status") in ("待复核", "交接失败")
        ]
        if unconfirmed:
            return None, (
                f"湖表 {table_name} 还有 {len(unconfirmed)} 条交接字段未确认，"
                "字段未确认前不能发给回填任务"
            )

        # 读取已确认的交接条目，按目标表的变更版本开始写入
        backfill_inputs: list[dict[str, Any]] = []
        for handover in handover_rows:
            if handover.get("status") != "已确认":
                continue
            change = store.find("schema_evolution", int(handover.get("来源变更ID", 0) or 0))
            handover["status"] = "已交接"
            handover["pending"] = False
            handover["abnormal"] = False
            if change is not None and change.get("status") == "已执行":
                # 已发布的字段变更不会被再次回填，只关闭条目
                continue
            if change is not None and change.get("status") == "待审批":
                change["status"] = "已执行"
                change["pending"] = False
                change["abnormal"] = False
            backfill_inputs.append({
                "来源变更ID": handover.get("来源变更ID"),
                "批次号": handover.get("批次号", ""),
                "湖表名": table_name,
                "变更字段": handover.get("变更字段", ""),
                "字段定义": handover.get("字段定义", ""),
                "变更版本": handover.get("变更版本", ""),
            })

        # 初始化检查点
        checkpoint = self._checkpoints.get(entry_id, 0)

        entry["status"] = "回填中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["处理记录数"] = str(checkpoint)

        handover_note = ""
        if backfill_inputs:
            # 回填记录保留实际使用的字段定义，从变更版本开始写入
            entry["回填输入"] = backfill_inputs
            entry["起始变更版本"] = backfill_inputs[0]["变更版本"]
            handover_note = (
                f"，接收交接字段 {len(backfill_inputs)} 个，"
                f"从变更版本 {entry['起始变更版本']} 开始写入"
            )
            store.log_event("backfill_handover_consumed", "backfill_handover", "backfill_job", {
                "task_id": entry_id,
                "table_name": table_name,
                "inputs": [
                    {"来源变更ID": item["来源变更ID"], "变更字段": item["变更字段"]}
                    for item in backfill_inputs
                ],
            })

        store.log_event("backfill_started", "backfill_job", "backfill_job", {
            "task_id": entry_id,
            "table_name": table_name,
            "time_range": time_range,
            "checkpoint": checkpoint,
        })

        return entry, (
            f"回填任务 {entry.get('任务名称')} 已启动，时间范围={time_range}，"
            f"检查点从 {checkpoint} 开始{handover_note}"
        )

    def checkpoint_backfill(self, entry_id: int, processed_count: int) -> tuple[dict[str, Any] | None, str]:
        """更新回填检查点，支持断点续做。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "回填中":
            return None, f"回填任务状态为{entry.get('status')}，不可保存检查点"

        self._checkpoints[entry_id] = processed_count
        entry["处理记录数"] = str(processed_count)

        store.log_event("backfill_checkpoint", "backfill_job", "backfill_job", {
            "task_id": entry_id,
            "checkpoint": processed_count,
        })

        return entry, f"回填任务检查点已更新：已处理 {processed_count} 条记录"

    def complete_backfill(self, entry_id: int, total_records: int) -> tuple[dict[str, Any] | None, str]:
        """完成回填任务，通知治理报告。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "回填中":
            return None, f"回填任务状态为{entry.get('status')}，不可完成"

        table_name = entry.get("目标湖表", "")

        entry["status"] = "已完成"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["处理记录数"] = str(total_records)

        # 清理检查点
        self._checkpoints.pop(entry_id, None)

        # 通知治理报告
        store.log_event("backfill_completed_to_govern", "backfill_job", "govern_report", {
            "task_id": entry_id,
            "table_name": table_name,
            "total_records": total_records,
        })

        return entry, f"回填任务 {entry.get('任务名称')} 已完成，总计 {total_records} 条记录"

    def fail_backfill(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """回填失败：保留检查点以便恢复。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "回填中":
            return None, f"回填任务状态为{entry.get('status')}，不可标记失败"

        checkpoint = self._checkpoints.get(entry_id, 0)

        entry["status"] = "回填失败"
        entry["pending"] = False
        entry["abnormal"] = True

        store.log_event("backfill_failed", "backfill_job", "retention_mgr", {
            "task_id": entry_id,
            "failed_at_checkpoint": checkpoint,
        })

        return entry, f"回填任务失败，检查点保留在 {checkpoint} 条，可从该位置恢复"

    def get_checkpoint(self, entry_id: int) -> int | None:
        """获取回填检查点。"""
        return self._checkpoints.get(entry_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "启动回填":
            return self.start_backfill(entry_id)
        return super().run_action(entry_id, action)