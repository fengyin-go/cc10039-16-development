"""回填字段交接清单端到端测试：批量交接、字典复核、回填联动与幂等。

场景对应值班流程：
1. 两个无数据依赖的湖表变更一次提交，生成同批次交接条目；
2. 执行前撤下一张表校验所需的字典版本，复核结果一张通过一张失败；
3. 分别从两张表发起回填：未确认的表被拦截，已确认的表按变更版本写入；
4. 补齐字典后重新处理，重试只推进未交接的条目；
5. 核对回填输入中的表与字段来自哪条变更，已发布的变更不会再次回填。

注意：store 是全局单例，测试自建数据（唯一表名/字典名），不依赖种子 id。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.schema_evolution import SchemaEvolutionService
from app.services.backfill_handover import BackfillHandoverService
from app.services.backfill_job import BackfillJobService
from app.services.data_dictionary import DataDictionaryService


def _make_approved_change(svc: SchemaEvolutionService, table: str, field: str) -> int:
    """登记一条 Schema 变更并提交审批，返回变更 id（状态=待审批）。"""
    entry, missing = svc.create_entry({
        "湖表名": table,
        "变更类型": "添加字段",
        "变更字段": field,
        "兼容级别": "向后兼容",
        "变更前快照": "",
    })
    assert entry is not None, f"变更登记失败：{missing}"
    approved, msg = svc.submit_approval(entry["id"])
    assert approved is not None, f"变更审批失败：{msg}"
    assert approved["status"] == "待审批"
    return entry["id"]


def _make_published_dict(svc: DataDictionaryService, name: str, version: str) -> int:
    """登记并发布一个字典版本，返回字典条目 id。"""
    entry, missing = svc.create_entry({
        "字典名称": name,
        "适用范围": "回填交接测试",
        "值域列表": "A,B,C",
        "版本号": version,
        "更新说明": "测试",
    })
    assert entry is not None, f"字典登记失败：{missing}"
    published, msg = svc.run_action(entry["id"], "发布字典")
    assert published is not None and published["status"] == "已发布", msg
    return entry["id"]


def _make_backfill_job(svc: BackfillJobService, name: str, table: str) -> int:
    """登记一个待调度的回填任务，返回任务 id。"""
    entry, missing = svc.create_entry({
        "任务名称": name,
        "目标湖表": table,
        "回填时间范围": "2026-11-01~2026-11-21",
    })
    assert entry is not None, f"回填任务登记失败：{missing}"
    return entry["id"]


class TestBackfillHandoverFlow:
    """两表批次：一张复核通过、一张字典失效，补齐后重试的完整链路。"""

    def setup_method(self):
        self.schema_svc = SchemaEvolutionService()
        self.handover_svc = BackfillHandoverService()
        self.backfill_svc = BackfillJobService()
        self.dict_svc = DataDictionaryService()

    def test_batch_handover_partial_failure_retry_and_trace(self):
        # ---- 1) 准备：两个无数据依赖的湖表变更 + 各自校验所需字典 ----
        table_a = "交接测试表A"
        table_b = "交接测试表B"
        change_a = _make_approved_change(self.schema_svc, table_a, "字段fa")
        change_b = _make_approved_change(self.schema_svc, table_b, "字段fb")
        dict_a = _make_published_dict(self.dict_svc, "交接测试字典A", "v1")
        dict_b = _make_published_dict(self.dict_svc, "交接测试字典B", "v1")

        # ---- 2) 值班员一次提交两条已审批变更 ----
        batch, msg = self.handover_svc.submit_batch([
            {"change_id": change_a, "字段定义": "fa BIGINT", "关联字典": "交接测试字典A", "字典版本": "v1"},
            {"change_id": change_b, "字段定义": "fb STRING", "关联字典": "交接测试字典B", "字典版本": "v1"},
        ])
        assert batch is not None, msg
        assert batch["created"] == 2
        batch_id = batch["batch_id"]
        entry_ids = [r["entry_id"] for r in batch["results"] if r["ok"]]
        assert len(entry_ids) == 2
        # 逐条知道对应的字段和湖表
        rows = [store.find("backfill_handover", eid) for eid in entry_ids]
        assert {r["湖表名"] for r in rows} == {table_a, table_b}
        assert {r["变更字段"] for r in rows} == {"字段fa", "字段fb"}
        assert all(r["批次号"] == batch_id for r in rows)
        assert all(r["status"] == "待复核" for r in rows)
        count_after_submit = len(store.rows("backfill_handover"))

        # ---- 3) 执行前撤下表B校验所需的字典版本 ----
        disabled, msg = self.dict_svc.run_action(dict_b, "停用字典")
        assert disabled is not None and disabled["status"] == "已停用", msg

        # ---- 4) 批量复核：同批次两张表复核结果不同 ----
        review, msg = self.handover_svc.review_pending(batch_id)
        assert review["confirmed"] == 1
        assert review["failed"] == 1
        by_table = {r["湖表名"]: r for r in review["results"]}
        assert by_table[table_a]["status"] == "已确认"
        assert by_table[table_b]["status"] == "交接失败"
        assert "字典" in by_table[table_b]["message"]

        # ---- 5) 分别从两张表发起回填 ----
        job_a = _make_backfill_job(self.backfill_svc, "回填任务A", table_a)
        job_b = _make_backfill_job(self.backfill_svc, "回填任务B", table_b)

        # 表B字段未确认，不能发给对应回填任务
        blocked, msg = self.backfill_svc.start_backfill(job_b)
        assert blocked is None
        assert "未确认" in msg

        # 表A补数窗口开启：按变更版本写入，回填记录保留实际字段定义
        started, msg = self.backfill_svc.start_backfill(job_a)
        assert started is not None, msg
        assert started["status"] == "回填中"
        inputs_a = started["回填输入"]
        assert len(inputs_a) == 1
        assert inputs_a[0]["来源变更ID"] == change_a
        assert inputs_a[0]["湖表名"] == table_a
        assert inputs_a[0]["变更字段"] == "字段fa"
        assert inputs_a[0]["字段定义"] == "fa BIGINT"
        assert started["起始变更版本"] == inputs_a[0]["变更版本"] == f"SE-{change_a}"
        # 交接条目与来源变更状态联动
        handover_a = [r for r in rows if r["湖表名"] == table_a][0]
        assert handover_a["status"] == "已交接"
        assert store.find("schema_evolution", change_a)["status"] == "已执行"

        # ---- 6) 补齐字典后重新处理：重试只推进未交接的那部分 ----
        republished, msg = self.dict_svc.run_action(dict_b, "发布字典")
        assert republished is not None and republished["status"] == "已发布", msg

        retry, msg = self.handover_svc.review_pending(batch_id)
        assert retry["confirmed"] == 1
        assert retry["failed"] == 0
        assert [r["湖表名"] for r in retry["results"]] == [table_b]
        # 表A的条目保持已交接，没有被重复推进
        assert handover_a["status"] == "已交接"
        assert len(store.rows("backfill_handover")) == count_after_submit  # 重试不产生新条目

        # 表B现在可以回填，回填输入来自变更B
        started_b, msg = self.backfill_svc.start_backfill(job_b)
        assert started_b is not None, msg
        inputs_b = started_b["回填输入"]
        assert len(inputs_b) == 1
        assert inputs_b[0]["来源变更ID"] == change_b
        assert inputs_b[0]["湖表名"] == table_b
        assert inputs_b[0]["变更字段"] == "字段fb"
        assert inputs_b[0]["字段定义"] == "fb STRING"
        assert store.find("schema_evolution", change_b)["status"] == "已执行"

        # ---- 7) 幂等：已发布的字段变更不会被再次回填 ----
        again, msg = self.handover_svc.submit_batch([{"change_id": change_a}])
        assert again is not None
        assert again["created"] == 0
        assert "已发布" in again["results"][0]["message"]

        # 表A再起一个回填任务：无未交接条目，正常启动但没有新的回填输入
        completed, msg = self.backfill_svc.complete_backfill(job_a, 100)
        assert completed is not None, msg
        job_a2 = _make_backfill_job(self.backfill_svc, "回填任务A2", table_a)
        started_a2, msg = self.backfill_svc.start_backfill(job_a2)
        assert started_a2 is not None, msg
        assert "回填输入" not in started_a2


class TestBackfillHandoverGuards:
    """交接清单的边界与幂等约束。"""

    def setup_method(self):
        self.schema_svc = SchemaEvolutionService()
        self.handover_svc = BackfillHandoverService()
        self.backfill_svc = BackfillJobService()

    def test_submit_batch_rejects_unapproved_change(self):
        """草稿状态的变更未审批，不能进入交接清单。"""
        entry, _ = self.schema_svc.create_entry({
            "湖表名": "交接守卫表1",
            "变更类型": "添加字段",
            "变更字段": "字段g1",
            "兼容级别": "向后兼容",
            "变更前快照": "",
        })
        assert entry is not None and entry["status"] == "草稿"
        batch, msg = self.handover_svc.submit_batch([{"change_id": entry["id"]}])
        assert batch is not None
        assert batch["created"] == 0
        assert "待审批" in batch["results"][0]["message"]

    def test_submit_batch_rejects_unknown_change(self):
        batch, msg = self.handover_svc.submit_batch([{"change_id": 999999}])
        assert batch is not None
        assert batch["created"] == 0
        assert "不存在" in batch["results"][0]["message"]

    def test_submit_batch_empty_items(self):
        batch, msg = self.handover_svc.submit_batch([])
        assert batch is None
        assert "至少" in msg

    def test_submit_batch_idempotent_for_active_entry(self):
        """同一变更已有活跃交接条目时不重复登记。"""
        change_id = _make_approved_change(self.schema_svc, "交接守卫表2", "字段g2")
        first, _ = self.handover_svc.submit_batch([{"change_id": change_id}])
        assert first is not None and first["created"] == 1
        second, _ = self.handover_svc.submit_batch([{"change_id": change_id}])
        assert second is not None
        assert second["created"] == 0
        assert "不重复" in second["results"][0]["message"]

    def test_review_action_via_run_action(self):
        """单条复核动作：通过通用动作入口触发。"""
        change_id = _make_approved_change(self.schema_svc, "交接守卫表3", "字段g3")
        batch, _ = self.handover_svc.submit_batch([{"change_id": change_id}])
        entry_id = batch["results"][0]["entry_id"]
        reviewed, msg = self.handover_svc.run_action(entry_id, "复核交接")
        assert reviewed is not None, msg
        assert reviewed["status"] == "已确认"
        # 已确认的条目不可重复复核
        again, msg2 = self.handover_svc.run_action(entry_id, "复核交接")
        assert again is None
        assert "无需复核" in msg2

    def test_review_closes_entry_when_change_already_published(self):
        """变更被单条路径执行后，残留待复核条目复核时直接关闭，不再回填。"""
        change_id = _make_approved_change(self.schema_svc, "交接守卫表4", "字段g4")
        batch, _ = self.handover_svc.submit_batch([{"change_id": change_id}])
        entry_id = batch["results"][0]["entry_id"]
        # 变更通过单条执行路径发布
        executed, msg = self.schema_svc.execute_change(change_id)
        assert executed is not None and executed["status"] == "已执行"
        reviewed, msg = self.handover_svc.review_entry(entry_id)
        assert reviewed is not None, msg
        assert reviewed["status"] == "已交接"
        assert "已发布" in msg

    def test_backfill_without_handover_entries_unaffected(self):
        """没有交接条目的湖表，回填启动保持原行为。"""
        job_id = _make_backfill_job(self.backfill_svc, "回填守卫任务", "无交接表")
        started, msg = self.backfill_svc.start_backfill(job_id)
        assert started is not None, msg
        assert started["status"] == "回填中"
        assert "回填输入" not in started
