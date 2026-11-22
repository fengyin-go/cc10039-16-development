"""回填字段交接清单领域服务：批量承接已审批的 Schema 变更，复核后交接给数据回填。

值班员一次提交多条已审批（待审批）的 Schema 变更，清单逐条记录对应的湖表、
字段、字段定义、变更版本和关联字典版本。复核时校验关联字典版本是否仍处于
已发布状态；只有通过复核（已确认）的字段才能被回填任务接收。
部分失败后重试只推进未交接的条目，已交接条目和已发布的变更不会被再次回填。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class BackfillHandoverService(BaseModuleService):
    MODULE = "backfill_handover"
    ENTITY = "交接条目"
    OPERATION_TERM = "交接"
    LIST_FIELDS = ["湖表名", "变更字段", "字段定义", "变更版本", "关联字典版本"]
    REQUIRED_FIELDS = ["湖表名", "变更字段"]
    STATUS_ORDER = ["待复核", "已确认", "交接失败", "已交接"]
    ACTION_RULES = {"复核交接": "已确认"}
    NEGATIVE_ACTIONS = []

    # 可以被复核（重试）推进的状态；已确认/已交接的条目不再重复处理
    REVIEWABLE_STATUSES = ("待复核", "交接失败")
    # 同一变更只允许存在一条活跃交接条目
    ACTIVE_STATUSES = ("待复核", "已确认")

    # ---- 批量交接提交 ----

    def submit_batch(self, items: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
        """值班员一次提交多条已审批的 Schema 变更，生成带回批次号的交接条目。

        每条 item：{"change_id": int, "字段定义"?, "变更版本"?, "关联字典"?, "字典版本"?}
        已发布（已执行）的变更直接跳过，不会对同一变更重复登记活跃条目。
        """
        if not items:
            return None, "批量交接至少需要一条已审批的 Schema 变更"

        batch_id = self._next_batch_id()
        results: list[dict[str, Any]] = []
        created = 0

        for item in items:
            try:
                change_id = int(item.get("change_id") or 0)
            except (TypeError, ValueError):
                change_id = 0
            change = store.find("schema_evolution", change_id)
            if change is None:
                results.append({"change_id": change_id, "ok": False, "message": "Schema 变更不存在"})
                continue

            change_status = change.get("status")
            if change_status == "已执行":
                results.append({
                    "change_id": change_id, "ok": False,
                    "message": "变更已发布，不会再次回填，跳过登记",
                })
                continue
            if change_status != "待审批":
                results.append({
                    "change_id": change_id, "ok": False,
                    "message": f"变更状态为{change_status}，仅已审批（待审批）的变更可批量交接",
                })
                continue

            existing = [
                row for row in store.rows(self.MODULE)
                if int(row.get("来源变更ID", 0) or 0) == change_id
                and row.get("status") in self.ACTIVE_STATUSES
            ]
            if existing:
                results.append({
                    "change_id": change_id, "ok": False,
                    "message": f"变更已存在交接条目 #{existing[0].get('id')}，不重复提交",
                })
                continue

            table_name = change.get("湖表名", "")
            field_name = change.get("变更字段", "")
            field_def = str(item.get("字段定义") or field_name)
            version = str(item.get("变更版本") or f"SE-{change_id}")
            dict_name = str(item.get("关联字典") or "")
            dict_version = str(item.get("字典版本") or "")
            dict_ref = f"{dict_name}@{dict_version}" if dict_name else ""

            rows = store.rows(self.MODULE)
            entry = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "批次号": batch_id,
                "湖表名": table_name,
                "变更字段": field_name,
                "字段定义": field_def,
                "变更版本": version,
                "关联字典版本": dict_ref,
                "字典名称": dict_name,
                "字典版本": dict_version,
                "来源变更ID": change_id,
                "status": "待复核",
                "pending": True,
                "abnormal": False,
            }
            rows.append(entry)
            created += 1
            results.append({
                "change_id": change_id,
                "ok": True,
                "entry_id": entry["id"],
                "湖表名": table_name,
                "变更字段": field_name,
                "message": "已登记回填字段交接清单",
            })

        store.log_event("handover_batch_submitted", "schema_evolution", "backfill_handover", {
            "batch_id": batch_id,
            "submitted": created,
            "total": len(items),
        })

        summary = f"批次 {batch_id}：提交 {len(items)} 条变更，登记交接条目 {created} 条"
        return {"batch_id": batch_id, "created": created, "results": results}, summary

    # ---- 复核（含重试） ----

    def review_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """复核单条交接条目：校验来源变更与关联字典版本。

        复核通过置为已确认；关联字典版本失效置为交接失败，可补齐字典后重试。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "交接条目不存在"
        if entry.get("status") not in self.REVIEWABLE_STATUSES:
            return None, f"交接条目状态为{entry.get('status')}，无需复核"

        table_name = entry.get("湖表名", "")
        field_name = entry.get("变更字段", "")

        change = store.find("schema_evolution", int(entry.get("来源变更ID", 0) or 0))
        if change is None:
            entry["status"] = "交接失败"
            entry["pending"] = True
            entry["abnormal"] = True
            return None, f"复核未通过：来源 Schema 变更 #{entry.get('来源变更ID')} 不存在"
        if change.get("status") == "已回滚":
            entry["status"] = "交接失败"
            entry["pending"] = True
            entry["abnormal"] = True
            return None, f"复核未通过：来源变更 {table_name} - {field_name} 已回滚"
        if change.get("status") == "已执行":
            # 字段变更已发布，无需再次回填，直接关闭条目
            entry["status"] = "已交接"
            entry["pending"] = False
            entry["abnormal"] = False
            store.log_event("handover_closed_published", "backfill_handover", "schema_evolution", {
                "entry_id": entry_id,
                "change_id": change.get("id"),
                "table_name": table_name,
            })
            return entry, f"来源变更 {table_name} - {field_name} 已发布，无需再次回填，条目关闭"

        dict_name = entry.get("字典名称", "")
        dict_version = entry.get("字典版本", "")
        if dict_name:
            matched = [
                d for d in store.rows("data_dictionary")
                if d.get("字典名称") == dict_name
                and str(d.get("版本号", "")) == str(dict_version)
            ]
            published = [d for d in matched if d.get("status") == "已发布"]
            if not published:
                entry["status"] = "交接失败"
                entry["pending"] = True
                entry["abnormal"] = True
                store.log_event("handover_review_failed", "backfill_handover", "data_dictionary", {
                    "entry_id": entry_id,
                    "table_name": table_name,
                    "dict_ref": f"{dict_name}@{dict_version}",
                })
                return None, f"复核未通过：关联字典版本失效（{dict_name}@{dict_version} 未处于已发布状态）"

        entry["status"] = "已确认"
        entry["pending"] = True
        entry["abnormal"] = False
        store.log_event("handover_confirmed", "backfill_handover", "backfill_job", {
            "entry_id": entry_id,
            "table_name": table_name,
            "field_name": field_name,
            "version": entry.get("变更版本", ""),
        })
        return entry, f"交接条目已确认：{table_name} - {field_name}，等待回填任务接收"

    def review_pending(self, batch_id: str | None = None) -> tuple[dict[str, Any], str]:
        """批量复核：只推进未交接的条目（待复核/交接失败），已交接的不重复处理。"""
        rows = [
            row for row in store.rows(self.MODULE)
            if row.get("status") in self.REVIEWABLE_STATUSES
        ]
        if batch_id:
            rows = [row for row in rows if row.get("批次号") == batch_id]

        results: list[dict[str, Any]] = []
        for row in rows:
            _, message = self.review_entry(int(row.get("id", 0)))
            results.append({
                "entry_id": row.get("id"),
                "批次号": row.get("批次号", ""),
                "湖表名": row.get("湖表名", ""),
                "变更字段": row.get("变更字段", ""),
                "status": row.get("status"),
                "ok": row.get("status") in ("已确认", "已交接"),
                "message": message,
            })

        confirmed = sum(1 for item in results if item["status"] == "已确认")
        failed = sum(1 for item in results if item["status"] == "交接失败")
        closed = sum(1 for item in results if item["status"] == "已交接")
        summary = (
            f"本次复核 {len(results)} 条：确认 {confirmed} 条，失败 {failed} 条，"
            f"已发布关闭 {closed} 条；已交接条目不再重复处理"
        )
        return {
            "results": results,
            "confirmed": confirmed,
            "failed": failed,
            "closed": closed,
        }, summary

    # ---- 供回填任务读取 ----

    def unconfirmed_for_table(self, table_name: str) -> list[dict[str, Any]]:
        """目标表仍未确认（待复核/交接失败）的交接条目。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("湖表名") == table_name
            and row.get("status") in self.REVIEWABLE_STATUSES
        ]

    def confirmed_for_table(self, table_name: str) -> list[dict[str, Any]]:
        """目标表已确认、等待回填任务接收的交接条目。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("湖表名") == table_name
            and row.get("status") == "已确认"
        ]

    # ---- 内部工具 ----

    def _next_batch_id(self) -> str:
        """按现有批次号递增生成新批次号。"""
        numbers: list[int] = []
        for row in store.rows(self.MODULE):
            numbers.extend(int(num) for num in re.findall(r"\d+", str(row.get("批次号", ""))))
        return f"HB-{(max(numbers) if numbers else 0) + 1:03d}"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "复核交接":
            return self.review_entry(entry_id)
        return super().run_action(entry_id, action)
