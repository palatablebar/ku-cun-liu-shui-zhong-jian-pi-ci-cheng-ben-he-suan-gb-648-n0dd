"""增量补丁：变化行合并基线、采信分类、改判单据、幂等重试、中断恢复。

取舍的背景：上游每天只回传有变化的流水行。整批替换会丢未变化的旧行，
等待全量重导会停住当天核算；补丁把变化行与当前基线按业务键合并后走同一
条质量 → 重放 → 成本 → 对账链，当天即可核算，且与全量重导结果一致。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import read_fixture, read_sample

CLEAN = "movements_clean.csv"


def import_clean(client) -> dict:
    response = client.post("/api/movements/import", data={"sample": CLEAN})
    assert response.status_code == 200, response.text
    return response.json()


def apply(client, fixture: str) -> dict:
    response = client.post(
        "/api/movements/patch",
        files={"file": (fixture, read_fixture(fixture))},
    )
    assert response.status_code == 200, response.text
    return response.json()


def apply_sample(client, sample: str) -> dict:
    response = client.post("/api/movements/patch", data={"sample": sample})
    assert response.status_code == 200, response.text
    return response.json()


def adopted_counts(payload: dict) -> dict:
    return {
        name: len(payload["patch"]["adopted"][name])
        for name in ("added", "upgraded", "conflict", "stale", "unchanged",
                     "invalid", "resolved")
    }


def test_patch_applies_delta_and_reports(client):
    import_clean(client)
    payload = apply(client, "patch_delta.csv")

    assert adopted_counts(payload) == {
        "added": 2,       # RK-4002 入库、DB-4001 调拨出边
        "upgraded": 1,    # RK-3001 版本 2 覆盖版本 1
        "conflict": 0,
        "stale": 0,
        "unchanged": 2,   # CK-3002 与 RK-3001 版本 1 原样重发
        "invalid": 1,     # RK-4003 数量是哨兵
        "resolved": 0,
    }
    upgrade = payload["patch"]["adopted"]["upgraded"][0]
    assert upgrade["doc_no"] == "RK-3001"
    assert upgrade["supersedes"] == 1

    # 合并集 = 基线 5 行 + 采信 4 行（一致行不重复进账）
    assert payload["counts"]["raw_rows"] == 9
    assert payload["counts"]["applied"] == 6
    assert payload["counts"]["quarantined"] == 2
    assert payload["counts"]["superseded"] == 1
    assert Decimal(payload["quantities"]["ending_qty"]) == Decimal("322.5000")
    assert Decimal(payload["quantities"]["in_qty"]) == Decimal("210.0000")
    assert Decimal(payload["cogs"]) == Decimal("600.0000")
    assert Decimal(payload["ending_value"]) == Decimal("3400.0000")
    assert payload["reconcile_ok"] is True

    reasons = {item["code"] for item in payload["quarantine"]}
    assert reasons == {"TRN_MISSING_LEG", "QTY_MISSING"}
    report = payload["patch"]
    assert report["baseline_before"] and report["baseline_after"]
    assert report["baseline_before"] != report["baseline_after"]
    assert report["previous_run_id"]


def test_patch_completes_transfer_leg_and_rejudges(client):
    import_clean(client)
    apply(client, "patch_delta.csv")
    payload = apply(client, "patch_leg.csv")

    assert adopted_counts(payload)["added"] == 1
    rejudged = {item["doc_no"]: item for item in payload["patch"]["rejudged"]}
    assert "DB-4001" in rejudged
    before = rejudged["DB-4001"]["before"]
    after = rejudged["DB-4001"]["after"]
    assert before == [{"status": "quarantine", "reason": "TRN_MISSING_LEG", "rows": 1}]
    assert after == [{"status": "valid", "reason": "", "rows": 2}]

    # 调拨双边齐全才占用库存：总库存不变，层移到目标仓
    assert Decimal(payload["quantities"]["ending_qty"]) == Decimal("322.5000")
    store = client.app.state.store
    layers = store.layers(payload["run_id"])
    moved = [layer for layer in layers
             if layer["doc_no"] == "DB-4001" and layer["warehouse"] == "WH-C"]
    assert len(moved) == 1
    assert Decimal(moved[0]["qty_in"]) == Decimal("20.0000")
    assert moved[0]["origin"] == "调拨"
    checks = {check["code"]: check["ok"] for check in payload["checks"]}
    assert checks["transfer"] is True
    assert payload["reconcile_ok"] is True


def test_patch_retry_is_idempotent(client):
    import_clean(client)
    first = apply(client, "patch_delta.csv")
    store = client.app.state.store
    before = {
        "movements": len(store.movements(first["run_id"])),
        "layers": len(store.layers(first["run_id"])),
        "segments": len(store.consumptions(first["run_id"])),
    }

    second = apply(client, "patch_delta.csv")
    assert second["run_id"] == first["run_id"]
    assert second["counts"] == first["counts"]
    assert second["quantities"] == first["quantities"]
    assert second["cogs"] == first["cogs"]
    # 重试时补丁行全部与基线一致（含首次带入基线的隔离行），没有新增采信
    assert adopted_counts(second) == {
        "added": 0, "upgraded": 0, "conflict": 0,
        "stale": 0, "unchanged": 6, "invalid": 0, "resolved": 0,
    }
    # 不新增流水行、成本层与消耗段
    assert len(store.movements(first["run_id"])) == before["movements"]
    assert len(store.layers(first["run_id"])) == before["layers"]
    assert len(store.consumptions(first["run_id"])) == before["segments"]
    assert len(store.patches()) == 1


def test_patch_conflict_quarantines_group_and_stays_stable(client):
    import_clean(client)
    apply(client, "patch_delta.csv")
    payload = apply(client, "patch_conflict.csv")

    assert adopted_counts(payload)["conflict"] == 1
    store = client.app.state.store
    rows = [row for row in store.movements(payload["run_id"])
            if row["doc_no"] == "RK-3001"]
    assert rows and all(row["reason"] == "DUP_DOC_CONFLICT" for row in rows)
    # 冲突未决，RK-3001 的 120kg 不入账
    assert Decimal(payload["quantities"]["ending_qty"]) == Decimal("202.5000")

    again = apply(client, "patch_conflict.csv")
    assert again["run_id"] == payload["run_id"]
    assert again["counts"] == payload["counts"]
    assert adopted_counts(again)["unchanged"] == 1


def test_patch_stale_version_not_adopted(client):
    import_clean(client)
    apply(client, "patch_delta.csv")
    payload = apply(client, "patch_stale.csv")

    adopted = adopted_counts(payload)
    assert adopted["stale"] == 1
    assert adopted["added"] == 0
    # 过旧版本只留证据（被取代），不改账
    assert Decimal(payload["quantities"]["ending_qty"]) == Decimal("322.5000")
    store = client.app.state.store
    stale = [row for row in store.movements(payload["run_id"])
             if row["doc_no"] == "RK-3001" and row["status"] == "superseded"]
    assert stale and stale[0]["raw_qty"] == "105"


def test_patch_infile_duplicates_classified_once(client):
    import_clean(client)
    body = (
        "单据号,行号,类型,仓库,物料编码,数量,单位,单价,发生时间,方向,关联单号,版本\n"
        "RK-5001,1,采购入库,WH-A,000123,10,kg,10.0000,2026-04-08T09:00:00+08:00,入,,1\n"
        "RK-5001,1,采购入库,WH-A,000123,10,kg,10.0000,2026-04-08T09:00:00+08:00,入,,1\n"
        "RK-5002,1,采购入库,WH-A,000123,5,kg,10.0000,2026-04-08T10:00:00+08:00,入,,1\n"
        "RK-5002,1,采购入库,WH-A,000123,7,kg,10.0000,2026-04-08T10:00:00+08:00,入,,1\n"
    ).encode("utf-8")
    response = client.post(
        "/api/movements/patch", files={"file": ("patch_dup.csv", body)})
    assert response.status_code == 200, response.text
    payload = response.json()
    adopted = adopted_counts(payload)
    # 文件内完全相同的第二行判一致；同版本不同值的第二行判冲突
    assert adopted["added"] == 2
    assert adopted["unchanged"] == 1
    assert adopted["conflict"] == 1
    store = client.app.state.store
    rows = [row for row in store.movements(payload["run_id"])
            if row["doc_no"] == "RK-5002"]
    assert {row["reason"] for row in rows} == {"DUP_DOC_CONFLICT"}

    retry = client.post(
        "/api/movements/patch", files={"file": ("patch_dup.csv", body)})
    assert retry.status_code == 200
    assert retry.json()["run_id"] == payload["run_id"]
    assert retry.json()["counts"] == payload["counts"]


def test_patch_interrupted_restores_previous(client, monkeypatch):
    import_clean(client)
    store = client.app.state.store
    run_before = store.latest_run()
    baseline_before = store.baseline()
    layers_before = store.layers(run_before["run_id"])

    def boom(*args, **kwargs):
        raise RuntimeError("模拟采信中断")

    monkeypatch.setattr(store, "save_run", boom)
    with pytest.raises(RuntimeError):
        client.post(
            "/api/movements/patch",
            files={"file": ("patch_delta.csv", read_fixture("patch_delta.csv"))})
    monkeypatch.undo()

    # 中断后完整旧版：批次、基线、层、补丁记录全部原样
    assert store.latest_run()["run_id"] == run_before["run_id"]
    assert store.baseline() == baseline_before
    assert store.layers(run_before["run_id"]) == layers_before
    assert store.patches() == []


def test_save_run_rolls_back_on_failure(client):
    import_clean(client)
    store = client.app.state.store
    run_before = store.latest_run()
    baseline_before = store.baseline()

    broken_run = dict(run_before)
    broken_run["run_id"] = "run-broken"
    bad_baseline = [
        {"doc_no": "X-1", "line_no": 1, "revision": 1, "fingerprint": "f0",
         "cells": {"qty": Decimal("1")}, "row_no": 1},
    ]
    with pytest.raises(TypeError):
        store.save_run(broken_run, [], [], [], [], [], baseline=bad_baseline)

    assert store.get_run("run-broken") is None
    assert store.baseline() == baseline_before
    assert store.latest_run()["run_id"] == run_before["run_id"]


def test_patch_empty_rejected(client):
    import_clean(client)
    store = client.app.state.store
    run_before = store.latest_run()
    response = client.post(
        "/api/movements/patch",
        files={"file": ("patch_empty.csv", read_sample("movements_header_only.csv"))})
    assert response.status_code == 400
    assert response.json()["error"] == "E_PATCH_EMPTY"
    assert store.latest_run()["run_id"] == run_before["run_id"]


def test_full_import_resets_baseline(client):
    import_clean(client)
    first = apply(client, "patch_delta.csv")
    # 整批重导同一文件后基线重置，再打同一补丁得到同一批次
    again_clean = import_clean(client)
    second = apply(client, "patch_delta.csv")
    assert second["run_id"] == first["run_id"]
    assert second["counts"] == first["counts"]
    assert adopted_counts(second) == adopted_counts(first)
    assert again_clean["run_id"] != first["run_id"]


def test_replay_after_patch_matches_run(client):
    import_clean(client)
    payload = apply(client, "patch_delta.csv")
    replay = client.post("/api/replay")
    assert replay.status_code == 200, replay.text
    outcome = replay.json()
    assert outcome["run_id"] == payload["run_id"]
    assert outcome["quantities"] == payload["quantities"]
    assert outcome["cogs"] == payload["cogs"]
    assert outcome["layers"] == payload["layers"]
    assert outcome["segments"] == payload["segments"]


def test_patch_exports_consistent(client):
    import_clean(client)
    apply(client, "patch_delta.csv")
    payload = apply(client, "patch_leg.csv")
    store = client.app.state.store

    manifest = client.get("/api/export/manifest.json").json()
    movements = manifest["inputs"]["movements"]
    assert movements["baseline_sha256"] == payload["patch"]["baseline_after"]
    assert movements["patch_sha256"] == payload["patch"]["sha256"]
    assert manifest["quantities"]["ending"] == payload["quantities"]["ending_qty"]
    assert manifest["cost"]["cogs"] == payload["cogs"]
    assert all(check["ok"] for check in manifest["checks"])

    layers_csv = client.get("/api/export/layers.csv").text
    assert len([line for line in layers_csv.strip().splitlines()[1:] if line]) == \
        len(store.layers(payload["run_id"]))
    cost_csv = client.get("/api/export/cost.csv").text
    assert "DB-4001" in cost_csv

    reconcile = client.get("/api/reconcile").json()
    assert reconcile["ok"] is True


def test_patches_listing_and_detail(client):
    import_clean(client)
    payload = apply(client, "patch_delta.csv")

    listing = client.get("/api/movements/patches").json()
    assert listing["count"] == 1
    item = listing["patches"][0]
    assert item["run_id"] == payload["run_id"]
    assert item["adopted"] == {
        "added": 2, "upgraded": 1, "conflict": 0,
        "stale": 0, "unchanged": 2, "invalid": 1, "resolved": 0,
    }

    detail = client.get(f"/api/movements/patches/{payload['run_id']}").json()
    assert detail["report"]["adopted"]["added"]
    missing = client.get("/api/movements/patches/run-nope")
    assert missing.status_code == 404


def test_patch_fix_resolves_quarantined_row(client):
    """历史隔离行留在账上，直到补丁送来同业务键的可采信行判修复。"""
    import_clean(client)
    apply(client, "patch_delta.csv")  # RK-4003 数量哨兵 → 隔离
    store = client.app.state.store

    fix = (
        "单据号,行号,类型,仓库,物料编码,数量,单位,单价,发生时间,方向,关联单号,版本\n"
        "RK-4003,1,采购入库,WH-A,000123,10,kg,10.0000,2026-04-07T09:00:00+08:00,入,,2\n"
    ).encode("utf-8")
    response = client.post(
        "/api/movements/patch", files={"file": ("patch_fix.csv", fix)})
    assert response.status_code == 200, response.text
    payload = response.json()
    adopted = adopted_counts(payload)
    assert adopted["resolved"] == 1   # 旧隔离行修复移出
    assert adopted["added"] == 1      # 修好后的行按新增采信
    assert payload["patch"]["adopted"]["resolved"][0]["reason"] == "QTY_MISSING"

    rows = [row for row in store.movements(payload["run_id"])
            if row["doc_no"] == "RK-4003"]
    assert len(rows) == 1 and rows[0]["status"] == "valid"
    rejudged = {item["doc_no"]: item for item in payload["patch"]["rejudged"]}
    assert rejudged["RK-4003"]["before"][0]["reason"] == "QTY_MISSING"
    assert rejudged["RK-4003"]["after"] == [{"status": "valid", "reason": "", "rows": 1}]
    # 修复的 10kg 入账
    assert Decimal(payload["quantities"]["ending_qty"]) == Decimal("332.5000")


def test_import_opening_after_patch_uses_merged_input(client):
    """补丁批次之后导入新期初：流水输入用合并集存档，不依赖补丁文件落盘。"""
    import_clean(client)
    apply(client, "patch_delta.csv")
    opening = (
        "仓库,物料编码,数量,单位,单价\n"
        "WH-A,000123,200,kg,10.0000\n"
        "WH-A,000456,50,kg,8.0000\n"
    ).encode("utf-8")
    response = client.post(
        "/api/movements/opening", files={"file": ("opening_new.csv", opening)})
    assert response.status_code == 200, response.text
    payload = response.json()
    # 期初变了，但补丁合入的行（RK-4002 50kg、RK-3001 版本 2 的 120kg）仍在账上
    assert Decimal(payload["quantities"]["opening_qty"]) == Decimal("250.0000")
    assert Decimal(payload["quantities"]["in_qty"]) == Decimal("210.0000")
    assert payload["reconcile_ok"] is True


def test_sample_patches_apply_against_demo_baseline(client):
    client.post("/api/demo/run")
    day1 = apply_sample(client, "patch_day1.csv")
    assert day1["reconcile_ok"] is True
    rejudged = {item["doc_no"] for item in day1["patch"]["rejudged"]}
    assert "DB-2006" in rejudged  # 缺边调拨补齐，改判采纳

    day2 = apply_sample(client, "patch_day2.csv")
    assert day2["reconcile_ok"] is True
    adopted = adopted_counts(day2)
    assert adopted["added"] == 2      # DB-3007 双边
    assert adopted["unchanged"] == 1  # RK-2019 版本 3 重发
    assert adopted["invalid"] == 1    # RK-3002 数量哨兵
