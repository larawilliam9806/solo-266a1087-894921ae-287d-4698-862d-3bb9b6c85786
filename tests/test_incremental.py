"""续算优化专项测试（/summaries）。

覆盖：
- 单汇总续算与同一批样组从头核算数值一致（空白样数据相同，含正负抵消）；
- 续算只读取底稿成员编号，逐组统计只从去重后新增样组读取；
- 无新增样组也用当前空白样扣减值/方差生成新汇总，已保存汇总原样返回；
- 升级前汇总（无聚合缓存）首次成功引用时补读一次原组统计，重启后复用；
- 多份汇总引用按实际样组去重，每个样组仍可单独查询/汇总；
- 资料缺失列出所缺编号、不生成汇总；指标/单位不一致 409。
"""
import math
import os
import sys
import tempfile

import pytest

# 必须在导入应用之前指定独立数据库（与 tests/test_api.py 的库隔离，避免编号冲突）
_fd, _db_path = tempfile.mkstemp(prefix="incremental-", suffix=".db")
os.close(_fd)
os.remove(_db_path)
os.environ["DB_PATH"] = _db_path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)


def make_blank(blank_id, deduction, variance):
    r = client.post(
        "/blanks",
        json={"blank_id": blank_id, "deduction": deduction, "variance": variance},
    )
    assert r.status_code == 201, r.text


def make_group(group_id, reading_sum, variance_sum, usages,
               analyte="lead", unit="ug/L"):
    r = client.post(
        "/groups",
        json={
            "group_id": group_id,
            "analyte": analyte,
            "unit": unit,
            "reading_sum": reading_sum,
            "variance_sum": variance_sum,
            "blank_usages": usages,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def summary(summary_id):
    r = client.get(f"/summaries/{summary_id}")
    assert r.status_code == 200, r.text
    return r.json()


# 每个用例之间不共享数据：用独立编号前缀避免冲突
def test_incremental_matches_from_scratch_bit_for_bit():
    make_blank("T1-B1", 2.0, 0.04)
    make_blank("T1-B2", 1.0, 0.09)
    make_group("T1-G1", 100.0, 5.0, {"T1-B1": 10})
    make_group("T1-G2", 50.0, 3.0, {"T1-B1": 5, "T1-B2": 5})

    # 底稿 S1（含 G1、G2）
    r = client.post("/summaries", json={"group_ids": ["T1-G1", "T1-G2"]})
    assert r.status_code == 201, r.text
    s1_id = r.json()["summary_id"]

    # 新增样组 G3、G4
    make_group("T1-G3", 30.0, 1.0, {"T1-B2": 5})
    make_group("T1-G4", -12.5, 2.25, {"T1-B1": 3})

    # 续算：只引用 S1 + 新样组
    r_inc = client.post(
        "/summaries",
        json={"summary_ids": [s1_id], "group_ids": ["T1-G3", "T1-G4"]},
    )
    assert r_inc.status_code == 201, r_inc.text
    inc = r_inc.json()

    # 从头：用同一批去重样组核算（空白样数据保持不变）
    r_full = client.post(
        "/summaries",
        json={"group_ids": ["T1-G1", "T1-G2", "T1-G3", "T1-G4"]},
    )
    assert r_full.status_code == 201, r_full.text
    full = r_full.json()

    # 位级一致
    assert inc["group_ids"] == full["group_ids"]
    for key in (
        "total_measurements",
        "reading_sum",
        "variance_sum",
        "blank_deduction_total",
        "corrected_mean",
        "standard_error",
    ):
        assert inc[key] == full[key], f"{key}: 续算 {inc[key]} != 从头 {full[key]}"
    assert inc["blanks"] == full["blanks"]


def test_incremental_with_positive_negative_cancellation():
    # 读数有正有负、易触发抵消舍入差异的取值
    make_blank("T2-B1", 0.3, 0.01)
    make_group("T2-G1", 1e12, 1.0, {"T2-B1": 4})
    make_group("T2-G2", -1e12 + 7.0, 1.0, {"T2-B1": 4})
    r = client.post("/summaries", json={"group_ids": ["T2-G1", "T2-G2"]})
    base_id = r.json()["summary_id"]

    make_group("T2-G3", 3.25, 0.5, {"T2-B1": 2})
    make_group("T2-G4", -0.125, 0.5, {"T2-B1": 2})

    inc = client.post(
        "/summaries", json={"summary_ids": [base_id],
                            "group_ids": ["T2-G3", "T2-G4"]}
    ).json()
    full = client.post(
        "/summaries",
        json={"group_ids": ["T2-G1", "T2-G2", "T2-G3", "T2-G4"]},
    ).json()

    assert inc["reading_sum"] == full["reading_sum"]
    assert inc["corrected_mean"] == full["corrected_mean"]
    assert inc["standard_error"] == full["standard_error"]
    # 手工核对抵消结果：读数总和 = 7 + 3.25 - 0.125 = 10.125
    assert inc["reading_sum"] == 10.125


def test_no_new_groups_still_creates_summary_with_current_blanks():
    make_blank("T3-B1", 2.0, 0.04)
    make_group("T3-G1", 100.0, 5.0, {"T3-B1": 10})
    base_id = client.post(
        "/summaries", json={"group_ids": ["T3-G1"]}
    ).json()["summary_id"]

    before = len(client.get("/summaries").json())

    # 更正空白样：扣减与方差都变
    r = client.put("/blanks/T3-B1", json={"deduction": 4.0, "variance": 0.16})
    assert r.status_code == 200

    # 没有新增样组，也要按当前空白样扣减值与方差生成新汇总
    r = client.post("/summaries", json={"summary_ids": [base_id]})
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["summary_id"] != base_id
    assert new["group_ids"] == ["T3-G1"]
    # 扣减 = 4*10 = 40；均值 = (100-40)/10 = 6
    assert new["blank_deduction_total"] == 40.0
    assert new["corrected_mean"] == 6.0
    # V = 0.16*100 + 5 = 21；SE = sqrt(21)/10
    assert new["standard_error"] == math.sqrt(21) / 10
    assert new["blanks"][0]["deduction"] == 4.0
    assert new["blanks"][0]["variance"] == 0.16

    # 已保存的底稿按原样返回（快照不随后续更正改变）
    old = summary(base_id)
    assert old["corrected_mean"] == (100 - 20) / 10
    assert old["blanks"][0]["deduction"] == 2.0
    assert old["blanks"][0]["variance"] == 0.04

    after = len(client.get("/summaries").json())
    assert after == before + 1


def test_legacy_summary_backfilled_once_and_reused_after_restart():
    make_blank("T4-B1", 1.5, 0.02)
    make_group("T4-G1", 40.0, 2.0, {"T4-B1": 4})
    make_group("T4-G2", 20.0, 1.0, {"T4-B1": 6})
    base_id = client.post(
        "/summaries", json={"group_ids": ["T4-G1", "T4-G2"]}
    ).json()["summary_id"]

    # 模拟升级前的库：删除该汇总的聚合缓存
    with connect() as conn:
        conn.execute("DELETE FROM summary_aggregates WHERE summary_id = ?", (base_id,))
    with connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM summary_aggregates WHERE summary_id = ?", (base_id,)
        ).fetchone() is None

    make_group("T4-G3", 8.0, 0.4, {"T4-B1": 2})

    # 首次成功引用旧汇总：补读一次原组统计并落盘
    r = client.post(
        "/summaries", json={"summary_ids": [base_id], "group_ids": ["T4-G3"]}
    )
    assert r.status_code == 201, r.text
    new_id = r.json()["summary_id"]

    with connect() as conn:
        row = conn.execute(
            "SELECT backfilled FROM summary_aggregates WHERE summary_id = ?",
            (base_id,),
        ).fetchone()
        assert row is not None and row["backfilled"] == 1

    # 数值仍与从头核算一致
    full = client.post(
        "/summaries", json={"group_ids": ["T4-G1", "T4-G2", "T4-G3"]}
    ).json()
    inc = summary(new_id)
    assert inc["reading_sum"] == full["reading_sum"]
    assert inc["corrected_mean"] == full["corrected_mean"]
    assert inc["standard_error"] == full["standard_error"]

    # 模拟重启：用全新连接（等价于重启后的进程）再次引用，缓存仍在且不重补
    r = client.post("/summaries", json={"summary_ids": [base_id]})
    assert r.status_code == 201
    with connect() as conn:
        row = conn.execute(
            "SELECT backfilled FROM summary_aggregates WHERE summary_id = ?",
            (base_id,),
        ).fetchone()
        assert row["backfilled"] == 1  # 仍是首次补读的记录，未重复补读

    # 真正起一个新进程视角：重新建连接读库，缓存行仍只有一条
    fresh = connect()
    try:
        rows = fresh.execute(
            "SELECT COUNT(*) AS c FROM summary_aggregates WHERE summary_id = ?",
            (base_id,),
        ).fetchone()
        assert rows["c"] == 1
    finally:
        fresh.close()


def test_incremental_reads_only_new_groups_for_per_group_stats(monkeypatch):
    make_blank("T5-B1", 2.0, 0.04)
    make_group("T5-G1", 100.0, 5.0, {"T5-B1": 10})
    make_group("T5-G2", 50.0, 3.0, {"T5-B1": 5})
    base_id = client.post(
        "/summaries", json={"group_ids": ["T5-G1", "T5-G2"]}
    ).json()["summary_id"]

    make_group("T5-G3", 30.0, 1.0, {"T5-B1": 5})

    read_group_ids: list[str] = []
    original = main_module.group_payload

    def spy_group_payload(db, group_id):
        read_group_ids.append(group_id)
        return original(db, group_id)

    monkeypatch.setattr(main_module, "group_payload", spy_group_payload)

    # 快路径：只引用一份汇总、不带新样组——底稿样组的逐组统计一律不读
    r = client.post("/summaries", json={"summary_ids": [base_id]})
    assert r.status_code == 201, r.text
    assert read_group_ids == []

    # 带新增样组：逐组统计只从去重后新增的样组读取，底稿 G1/G2 不展开
    read_group_ids.clear()
    r = client.post(
        "/summaries", json={"summary_ids": [base_id], "group_ids": ["T5-G3"]}
    )
    assert r.status_code == 201, r.text
    assert read_group_ids == ["T5-G3"]

    # 对照：引用多份汇总时仍按实际样组展开，逐组读取去重后的样组
    read_group_ids.clear()
    r = client.post(
        "/summaries",
        json={"summary_ids": [base_id, r.json()["summary_id"]],
              "group_ids": ["T5-G3"]},
    )
    assert r.status_code == 201, r.text
    assert sorted(read_group_ids) == ["T5-G1", "T5-G2", "T5-G3"]


def test_multiple_summaries_dedup_by_actual_groups():
    make_blank("T6-B1", 1.0, 0.01)
    make_group("T6-G1", 10.0, 1.0, {"T6-B1": 3})
    make_group("T6-G2", 20.0, 2.0, {"T6-B1": 3})
    make_group("T6-G3", 30.0, 3.0, {"T6-B1": 3})

    s_a = client.post(
        "/summaries", json={"group_ids": ["T6-G1", "T6-G2"]}
    ).json()["summary_id"]
    s_b = client.post(
        "/summaries", json={"group_ids": ["T6-G2", "T6-G3"]}
    ).json()["summary_id"]

    # 两份汇总重叠 G2：去重后实际样组 {G1,G2,G3}
    r = client.post("/summaries", json={"summary_ids": [s_a, s_b]})
    assert r.status_code == 201, r.text
    merged = r.json()
    assert merged["group_ids"] == ["T6-G1", "T6-G2", "T6-G3"]
    assert merged["total_measurements"] == 9
    assert merged["reading_sum"] == 60.0

    # 与直接给全部样组的从头核算完全一致
    full = client.post(
        "/summaries", json={"group_ids": ["T6-G1", "T6-G2", "T6-G3"]}
    ).json()
    assert merged["corrected_mean"] == full["corrected_mean"]
    assert merged["standard_error"] == full["standard_error"]

    # 每个样组仍可单独查询和汇总
    assert client.get("/groups/T6-G2").status_code == 200
    one = client.post(
        "/summaries", json={"group_ids": ["T6-G2"]}
    ).json()
    assert one["group_ids"] == ["T6-G2"]


def test_missing_data_lists_ids_and_creates_nothing():
    make_blank("T7-B1", 1.0, 0.01)
    make_group("T7-G1", 10.0, 1.0, {"T7-B1": 2})
    base_id = client.post(
        "/summaries", json={"group_ids": ["T7-G1"]}
    ).json()["summary_id"]
    before = len(client.get("/summaries").json())

    # 缺失汇总号
    r = client.post("/summaries", json={"summary_ids": [999001]})
    assert r.status_code == 404
    assert r.json()["detail"]["missing"] == {"summary_ids": [999001]}

    # 缺失直接样组号（续算请求里带的新样组缺失）
    r = client.post(
        "/summaries", json={"summary_ids": [base_id], "group_ids": ["T7-NOPE"]}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["missing"] == {"group_ids": ["T7-NOPE"]}

    # 空白样缺失：旧底稿引用了未登记空白样
    make_group("T7-G9", 5.0, 0.2, {"T7-B9": 2})
    r = client.post("/summaries", json={"group_ids": ["T7-G9"]})
    assert r.status_code == 404
    assert r.json()["detail"]["missing"] == {"blank_ids": ["T7-B9"]}

    assert len(client.get("/summaries").json()) == before


def test_inconsistent_analyte_unit_still_409():
    make_blank("T8-B1", 1.0, 0.01)
    make_group("T8-G1", 10.0, 1.0, {"T8-B1": 2})
    base_id = client.post(
        "/summaries", json={"group_ids": ["T8-G1"]}
    ).json()["summary_id"]
    make_group("T8-G2", 1.0, 0.1, {"T8-B1": 2}, analyte="lead", unit="mg/L")

    r = client.post(
        "/summaries", json={"summary_ids": [base_id], "group_ids": ["T8-G2"]}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "inconsistent_analyte_or_unit"


def test_request_and_response_format_unchanged():
    make_blank("T9-B1", 2.0, 0.04)
    make_group("T9-G1", 100.0, 5.0, {"T9-B1": 10})
    r = client.post("/summaries", json={"group_ids": ["T9-G1"]})
    payload = r.json()
    assert set(payload) == {
        "summary_id", "analyte", "unit", "group_ids",
        "source_group_ids", "source_summary_ids",
        "total_measurements", "reading_sum", "variance_sum",
        "blank_deduction_total", "corrected_mean", "standard_error",
        "blanks", "created_at",
    }
    assert payload["source_group_ids"] == ["T9-G1"]
    assert payload["source_summary_ids"] == []
    assert payload["blanks"][0].keys() == {
        "blank_id", "usage_count", "deduction", "variance"
    }

    # 续算请求沿用同一请求体格式
    r2 = client.post("/summaries", json={"summary_ids": [payload["summary_id"]]})
    assert r2.status_code == 201
    assert r2.json()["source_summary_ids"] == [payload["summary_id"]]
    assert r2.json()["source_group_ids"] == []


def test_backfill_rolls_back_when_request_fails_then_succeeds():
    make_blank("T10-B1", 1.0, 0.01)
    make_group("T10-G1", 10.0, 1.0, {"T10-B1": 2})
    base_id = client.post(
        "/summaries", json={"group_ids": ["T10-G1"]}
    ).json()["summary_id"]
    with connect() as conn:
        conn.execute("DELETE FROM summary_aggregates WHERE summary_id = ?", (base_id,))
        conn.commit()

    # 请求失败（新增样组缺失）：不生成汇总，补读也不应落盘
    r = client.post(
        "/summaries", json={"summary_ids": [base_id], "group_ids": ["T10-MISSING"]}
    )
    assert r.status_code == 404
    with connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM summary_aggregates WHERE summary_id = ?", (base_id,)
        ).fetchone() is None

    # 随后成功引用：仍能补读并落盘
    r = client.post("/summaries", json={"summary_ids": [base_id]})
    assert r.status_code == 201
    with connect() as conn:
        row = conn.execute(
            "SELECT backfilled FROM summary_aggregates WHERE summary_id = ?",
            (base_id,),
        ).fetchone()
        assert row["backfilled"] == 1


def test_backfill_idempotent_and_reused_across_connections():
    make_blank("T11-B1", 1.0, 0.01)
    make_group("T11-G1", 12.0, 2.0, {"T11-B1": 3})
    make_group("T11-G2", 8.0, 1.0, {"T11-B1": 1})
    base_id = client.post(
        "/summaries", json={"group_ids": ["T11-G1", "T11-G2"]}
    ).json()["summary_id"]
    with connect() as conn:
        conn.execute("DELETE FROM summary_aggregates WHERE summary_id = ?", (base_id,))
        conn.commit()

    # 多次引用：第一次补读，之后全部走缓存
    for _ in range(3):
        r = client.post("/summaries", json={"summary_ids": [base_id]})
        assert r.status_code == 201

    fresh = connect()
    try:
        rows = fresh.execute(
            "SELECT reading_num, reading_den, backfilled, blank_usages_json "
            "FROM summary_aggregates WHERE summary_id = ?",
            (base_id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["backfilled"] == 1
        # 分量以十进制字符串保存，任意精度都不会溢出
        assert (rows[0]["reading_num"], rows[0]["reading_den"]) == ("20", "1")
        import json as _json
        assert _json.loads(rows[0]["blank_usages_json"]) == {"T11-B1": 4}
    finally:
        fresh.close()


def test_non_finite_values_rejected_with_422_not_500():
    # HTTP/JSON 层通常无法表达 inf/nan；直接验证汇总取数环节的防护，
    # 保证不会因 Fraction 构造失败而冒出 500
    from fastapi import HTTPException

    from app.main import frac_of

    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(HTTPException) as exc:
            frac_of(bad)
        assert exc.value.status_code == 422
        assert exc.value.detail["error"] == "non_finite_value"
    # 正常值不受影响
    assert float(frac_of(3.5)) == 3.5


def test_direct_group_overlapping_base_is_deduped_not_reread(monkeypatch):
    make_blank("T13-B1", 1.0, 0.01)
    make_group("T13-G1", 10.0, 1.0, {"T13-B1": 2})
    make_group("T13-G2", 20.0, 2.0, {"T13-B1": 2})
    base_id = client.post(
        "/summaries", json={"group_ids": ["T13-G1", "T13-G2"]}
    ).json()["summary_id"]

    read_group_ids: list[str] = []
    original = main_module.group_payload

    def spy(db, group_id):
        read_group_ids.append(group_id)
        return original(db, group_id)

    monkeypatch.setattr(main_module, "group_payload", spy)
    # 直接样组与底稿重叠（G1）：去重后无新增，逐组统计一次都不读
    r = client.post(
        "/summaries", json={"summary_ids": [base_id], "group_ids": ["T13-G1"]}
    )
    assert r.status_code == 201
    assert read_group_ids == []
    assert r.json()["group_ids"] == ["T13-G1", "T13-G2"]
    assert r.json()["reading_sum"] == 30.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
