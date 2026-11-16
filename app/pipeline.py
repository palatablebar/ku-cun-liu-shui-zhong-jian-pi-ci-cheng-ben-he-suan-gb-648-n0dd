"""平台编排：采信 → 质量 → 重放 → 成本 → 盘点对账。

模块之间只通过本文件衔接，模块内部不互相 import 规则函数；同一组输入得到同一 run_id，
重跑先删后插，行数、合计与首跑一致。

整批导入（run_chain）与增量补丁（apply_patch）共用同一条后半链：补丁把变化行
与当前基线合并后，走完全相同的质量裁决、时序重放、成本与对账，因此批次层、
单据成本、期末库存与导出在两条路径下同源一致。
"""
from __future__ import annotations

import time
from decimal import Decimal

from . import reasons
from .config import VALUE_QUANTUM, Settings
from .errors import DomainError
from .manifest import make_run_id, sha256_hex, utc_now_iso
from .modules.m1_ingest import patch as patch_mod
from .modules.m1_ingest import service as m1_service
from .modules.m2_doc_rules import contract
from .modules.m2_doc_rules import service as m2_service
from .modules.m3_quality import service as m3_service
from .modules.m4_replay import service as m4_service
from .modules.m6_reconcile import balance as balance_mod
from .modules.m6_reconcile import service as m6_service

DEFAULT_MOVEMENTS = "movements_dirty.csv"
DEFAULT_OPENING = "opening_balance.csv"
DEFAULT_COUNTS = "physical_counts.csv"

TRANSFER_REASONS = (
    "TRN_MISSING_LEG",
    "TRN_QTY_MISMATCH",
    "TRN_SKU_MISMATCH",
    "TRN_SAME_WAREHOUSE",
    "TRN_LEG_AMBIGUOUS",
    "TRN_LEG_BLOCKED",
)

# 补丁文件的这些状态说明没有可采信的数据行，直接拒绝，基线与批次不动。
PATCH_EMPTY_STATES = ("EMPTY_FILE", "HEADER_MISSING", "HEADER_ONLY", "ALL_BLANK")


def _fmt(value) -> str:
    """日志里只显示四位小数，避免八位尾数刷屏。"""
    return format(Decimal(value).quantize(VALUE_QUANTUM), "f")


def _make_log() -> tuple[list[dict], callable]:
    log: list[dict] = []

    def note(level: str, stage: str, message: str) -> None:
        log.append(
            {
                "seq": len(log) + 1,
                "at_utc": utc_now_iso(),
                "level": level,
                "stage": stage,
                "message": message,
            }
        )

    return log, note


def _load(settings: Settings, filename: str, raw: bytes | None, aliases: dict):
    """解析一份输入；raw 为空时从样例目录按文件名读取。返回 (解析结果, 原始字节)。"""
    if raw is None:
        filename, raw = m1_service.resolve_sample(settings, filename)
    parsed = m1_service.read_file(settings, filename, raw, aliases)
    return parsed, raw


def _stages(settings: Settings, note, rows: list[dict], meta: dict,
            opening: tuple, counts: tuple) -> dict:
    """后半链：期初 → 重放 → 实盘 → 批次行 → 对账。纯计算，不写库。"""
    parsed_opening, opening_bytes = _load(
        settings, opening[0], opening[1], contract.aliases("opening"))
    opening_rows = m2_service.read_opening(
        settings, parsed_opening["filename"], opening_bytes)["rows"]
    note(
        "INFO", "采信",
        f"{parsed_opening['filename']} 期初行 {len(opening_rows)}"
        f" 有效 {sum(1 for row in opening_rows if row['status'] == 'valid')}",
    )

    outcome = m4_service.replay_rows(settings, rows, opening_rows)
    totals = outcome["totals"]
    note(
        "INFO", "重放",
        f"调拨对 {outcome['pairs']} 层 {len(outcome['layers'])}"
        f" 消耗段 {len(outcome['segments'])}",
    )
    note(
        "WARN" if any(row["reason"] == "OUT_EXCEEDS_STOCK" for row in rows) else "INFO",
        "重放",
        f"出库超在手 {sum(1 for row in rows if row['reason'] == 'OUT_EXCEEDS_STOCK')}"
        f" 调拨问题 {sum(1 for row in rows if row['reason'] in TRANSFER_REASONS)}",
    )

    parsed_counts, counts_bytes = _load(
        settings, counts[0], counts[1], contract.aliases("counts"))
    count_input = m2_service.read_counts(
        settings, parsed_counts["filename"], counts_bytes)
    count_outcome = m6_service.build_records(
        count_input["rows"], outcome["layers"], settings)
    count_records = count_outcome["records"]
    note(
        "INFO", "对账",
        f"{parsed_counts['filename']} 盘点行 {len(count_records)}"
        f" 差异 {sum(1 for item in count_records if item['status'] == 'COUNT_DIFF')}"
        f" 拒绝 {sum(1 for item in count_records if item['status'] == 'COUNT_SHORT')}",
    )

    applied = sum(1 for row in rows if row["status"] == "valid")
    run = {
        "run_id": make_run_id(
            meta["movements_sha256"], parsed_opening["sha256"],
            parsed_counts["sha256"], settings.rule_version, settings.params()),
        "created_at": utc_now_iso(),
        "rule_version": settings.rule_version,
        "params": settings.params(),
        "movements_sha256": meta["movements_sha256"],
        "opening_sha256": parsed_opening["sha256"],
        "counts_sha256": parsed_counts["sha256"],
        "movements_file": meta["movements_file"],
        "opening_file": parsed_opening["filename"],
        "counts_file": parsed_counts["filename"],
        "movements_bytes": meta["movements_bytes"],
        "opening_bytes": len(opening_bytes),
        "counts_bytes": len(counts_bytes),
        "raw_rows": len(rows),
        "applied": applied,
        "quarantined": sum(1 for row in rows if row["status"] == "quarantine"),
        "superseded": sum(1 for row in rows if row["status"] == "superseded"),
        "duplicate": sum(1 for row in rows if row["status"] == "duplicate"),
        "blank": meta["blank"],
        "segments": meta["segments"],
        "opening_qty": totals["opening_qty"],
        "in_qty": totals["in_qty"],
        "out_qty": totals["out_qty"],
        "adj_in_qty": totals["adj_in_qty"],
        "adj_out_qty": totals["adj_out_qty"],
        "ending_qty": totals["ending_qty"],
        "cogs": totals["cogs"],
        "ending_value": totals["ending_value"],
        "baseline_sha256": meta.get("baseline_sha256", ""),
        "patch_sha256": meta.get("patch_sha256", ""),
        "status": "ready" if applied else "empty",
        "duration_ms": 0,
    }
    report = balance_mod.reconcile(
        run, rows, outcome["layers"], outcome["segments"], count_records,
        count_outcome["totals"]["adjusted_qty"],
    )
    note(
        "INFO", "对账",
        f"六本账 {'通过' if report['ok'] else '有差异'}"
        f" 期末 {_fmt(totals['ending_qty'])} kg",
    )
    note(
        "INFO", "成本",
        f"出库成本 {_fmt(totals['cogs'])} 期末金额 {_fmt(totals['ending_value'])}",
    )
    return {
        "run": run,
        "rows": rows,
        "outcome": outcome,
        "count_records": count_records,
        "report": report,
        "opening_bytes": opening_bytes,
        "counts_bytes": counts_bytes,
    }


def run_chain(settings: Settings, store, movements=None, opening=None,
              counts=None, movement_records: list | None = None) -> dict:
    """跑完整主流程；三份输入可分别给 (文件名, 字节) 或留空用样例。

    movement_records 给定时直接用它作为流水输入（补丁批次的合并集），
    不再按文件重读——上传的补丁文件本就不在样例目录。
    """
    started = time.monotonic()
    log, note = _make_log()

    mov_name, mov_raw = movements if movements else (DEFAULT_MOVEMENTS, None)
    open_name, open_raw = opening if opening else (DEFAULT_OPENING, None)
    count_name, count_raw = counts if counts else (DEFAULT_COUNTS, None)

    if movement_records is not None:
        records = movement_records
        merged_bytes = patch_mod.canonical_bytes(records)
        movements_bytes = merged_bytes
        movements_input = {"movements_merged": merged_bytes}
        mov_meta = {
            "movements_file": mov_name,
            "movements_sha256": sha256_hex(merged_bytes),
            "movements_bytes": len(merged_bytes),
            "blank": 0,
            "segments": 0,
        }
        note("INFO", "采信", f"{mov_name} 合并集 {len(records)} 行（存档输入）")
    else:
        parsed_movements, movements_bytes = _load(
            settings, mov_name, mov_raw, contract.aliases("movements"))
        records = parsed_movements["records"]
        movements_input = {"movements": movements_bytes}
        mov_meta = {
            "movements_file": parsed_movements["filename"],
            "movements_sha256": parsed_movements["sha256"],
            "movements_bytes": len(movements_bytes),
            "blank": parsed_movements["blank_rows"],
            "segments": parsed_movements["segments"],
        }
        note(
            "INFO", "采信",
            f"{parsed_movements['filename']} 编码 {parsed_movements['encoding']}"
            f" 段 {parsed_movements['segments']} 数据行 {parsed_movements['raw_rows']}"
            f" 空行 {parsed_movements['blank_rows']}"
            f" 状态 {parsed_movements['state'] or 'OK'}",
        )
        if parsed_movements["state"]:
            note("WARN", "采信", f"输入状态 {parsed_movements['state']}")

    built = m3_service.build(
        settings, {"records": records, "state": None, "blank_rows": mov_meta["blank"]})
    rows = built["rows"]
    note(
        "INFO", "质量",
        f"采纳 {sum(1 for row in rows if row['status'] == 'valid')}"
        f" 隔离 {sum(1 for row in rows if row['status'] == 'quarantine')}"
        f" 被取代 {sum(1 for row in rows if row['status'] == 'superseded')}"
        f" 重复 {sum(1 for row in rows if row['status'] == 'duplicate')}",
    )
    # 整批导入即重置基线：本文件里所有行（含未解决的隔离行）构成新基线。
    baseline = patch_mod.baseline_entries(records, rows)

    staged = _stages(
        settings, note, rows,
        {**mov_meta, "baseline_sha256": patch_mod.baseline_digest(baseline)},
        (open_name, open_raw), (count_name, count_raw),
    )
    run = staged["run"]
    run["duration_ms"] = int((time.monotonic() - started) * 1000)
    note("INFO", "完成", f"运行 {run['run_id']} 用时 {run['duration_ms']} ms")

    store.save_run(
        run, rows, staged["outcome"]["layers"], staged["outcome"]["segments"],
        staged["count_records"], log,
        baseline=baseline,
        inputs={
            **movements_input,
            "opening": staged["opening_bytes"],
            "counts": staged["counts_bytes"],
        },
    )
    return summary(run, rows, staged["outcome"], staged["count_records"],
                   staged["report"])


def _previous_input(settings: Settings, store, previous: dict | None,
                    kind: str, default_name: str) -> tuple:
    """补丁批次的期初/实盘沿用上一批次：优先该批次的输入存档，其次登记的样例名。"""
    if previous:
        blob = store.run_input(previous["run_id"], kind)
        if blob is not None:
            return previous.get(f"{kind}_file") or default_name, blob
        name = previous.get(f"{kind}_file")
        if name:
            return name, None
    return default_name, None


def apply_patch(settings: Settings, store, filename: str, raw: bytes) -> dict:
    """应用增量补丁：变化行与当前基线合并后重跑后半链，返回采信报告与批次摘要。

    - 采信分类（added/upgraded/conflict/stale/unchanged/invalid）逐行可查；
    - 改判单据（rejudged）对比前后两个批次的单据判定；
    - 所有计算完成后才落库，且落库是单事务：中断即回到完整旧版；
    - 合并集 canonical 化进 run_id：同一补丁重试得到同一批次，不新增流水或层。
    """
    started = time.monotonic()
    log, note = _make_log()

    parsed = m1_service.read_file(settings, filename, raw, contract.aliases("movements"))
    if parsed["state"] in PATCH_EMPTY_STATES:
        raise DomainError(
            "E_PATCH_EMPTY",
            f"补丁没有可采信的数据行（{parsed['state']}），基线与批次未改动", 400)
    note(
        "INFO", "采信",
        f"补丁 {parsed['filename']} 编码 {parsed['encoding']}"
        f" 段 {parsed['segments']} 数据行 {parsed['raw_rows']}"
        f" 空行 {parsed['blank_rows']}",
    )

    previous = store.latest_run()
    baseline = store.baseline()
    note("INFO", "补丁", f"当前基线 {len(baseline)} 行")

    norms = [
        m2_service.normalize_movement({**record, "seq": index}, settings)
        for index, record in enumerate(parsed["records"], 1)
    ]
    classified = patch_mod.classify(baseline, parsed["records"], norms)
    adopted = classified["report"]
    note(
        "INFO", "补丁",
        f"新增 {len(adopted['added'])} 升级 {len(adopted['upgraded'])}"
        f" 冲突 {len(adopted['conflict'])} 过旧 {len(adopted['stale'])}"
        f" 一致 {len(adopted['unchanged'])} 无效 {len(adopted['invalid'])}"
        f" 修复 {len(adopted['resolved'])}",
    )

    tagged = [
        (patch_mod.entry_to_record(entry), entry["doc_no"], entry["line_no"],
         entry["revision"])
        for entry in classified["carry"]
    ]
    tagged += [
        (record, norm["doc_no"], norm["line_no"], norm["revision"])
        for record, norm in classified["keep"]
    ]
    merged_records = patch_mod.canonical_order(tagged)
    merged_bytes = patch_mod.canonical_bytes(merged_records)

    built = m3_service.build(
        settings,
        {
            "records": merged_records,
            "state": None,
            "blank_rows": parsed["blank_rows"],
        },
    )
    rows = built["rows"]
    note(
        "INFO", "质量",
        f"合并集 {len(rows)} 行：采纳 {sum(1 for row in rows if row['status'] == 'valid')}"
        f" 隔离 {sum(1 for row in rows if row['status'] == 'quarantine')}"
        f" 被取代 {sum(1 for row in rows if row['status'] == 'superseded')}",
    )
    new_baseline = patch_mod.baseline_entries(merged_records, rows)

    staged = _stages(
        settings, note, rows,
        {
            "movements_file": parsed["filename"],
            "movements_sha256": sha256_hex(merged_bytes),
            "movements_bytes": len(merged_bytes),
            "blank": parsed["blank_rows"],
            "segments": parsed["segments"],
            "baseline_sha256": patch_mod.baseline_digest(new_baseline),
            "patch_sha256": sha256_hex(raw),
        },
        _previous_input(settings, store, previous, "opening", DEFAULT_OPENING),
        _previous_input(settings, store, previous, "counts", DEFAULT_COUNTS),
    )
    run = staged["run"]

    old_rows = store.movements(previous["run_id"]) if previous else []
    rejudged = patch_mod.diff_doc_verdicts(old_rows, rows)
    if rejudged:
        docs = "、".join(item["doc_no"] for item in rejudged[:3])
        note("WARN", "补丁", f"改判单据 {len(rejudged)}：{docs}")
    else:
        note("INFO", "补丁", "没有单据改判")

    report = {
        "filename": parsed["filename"],
        "sha256": sha256_hex(raw),
        "bytes": len(raw),
        "encoding": parsed["encoding"],
        "segments": parsed["segments"],
        "blank_rows": parsed["blank_rows"],
        "rows": parsed["raw_rows"],
        "baseline_before": patch_mod.baseline_digest(baseline) if baseline else None,
        "baseline_after": run["baseline_sha256"],
        "previous_run_id": previous["run_id"] if previous else None,
        "adopted": adopted,
        "rejudged": rejudged,
    }

    run["duration_ms"] = int((time.monotonic() - started) * 1000)
    note("INFO", "完成", f"运行 {run['run_id']} 用时 {run['duration_ms']} ms")

    store.save_run(
        run, rows, staged["outcome"]["layers"], staged["outcome"]["segments"],
        staged["count_records"], log,
        baseline=new_baseline,
        inputs={
            "movements_merged": merged_bytes,
            "opening": staged["opening_bytes"],
            "counts": staged["counts_bytes"],
        },
        patch={
            "run_id": run["run_id"],
            "patch_sha256": report["sha256"],
            "filename": parsed["filename"],
            "applied_at": run["created_at"],
            "report": report,
        },
    )
    result = summary(run, rows, staged["outcome"], staged["count_records"],
                     staged["report"])
    result["patch"] = report
    return result


def summary(run: dict, rows: list[dict], outcome: dict,
            count_records: list[dict], report: dict) -> dict:
    totals = outcome["totals"]
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "created_at": run["created_at"],
        "counts": {
            "raw_rows": len(rows),
            "applied": run["applied"],
            "quarantined": run["quarantined"],
            "superseded": run["superseded"],
            "duplicate": run["duplicate"],
            "blank": run["blank"],
            "segments": run["segments"],
        },
        "quantities": {
            key: format(totals[key], "f")
            for key in ("opening_qty", "in_qty", "out_qty", "adj_in_qty",
                        "adj_out_qty", "ending_qty")
        },
        "cogs": format(totals["cogs"], "f"),
        "ending_value": format(totals["ending_value"], "f"),
        "layers": len(outcome["layers"]),
        "segments": len(outcome["segments"]),
        "counts_rows": len(count_records),
        "reconcile_ok": report["ok"],
        "checks": report["checks"],
        "quarantine": reasons.distribution(
            [row for row in rows if row["status"] == "quarantine"]),
        "movements_sha256": run["movements_sha256"],
        "opening_sha256": run["opening_sha256"],
        "counts_sha256": run["counts_sha256"],
    }


def run_demo(settings: Settings, store) -> dict:
    return run_chain(settings, store)


def import_movements(settings: Settings, store, filename: str, raw: bytes) -> dict:
    previous = store.latest_run() or {}
    return run_chain(
        settings, store,
        movements=(filename, raw),
        opening=(previous.get("opening_file") or DEFAULT_OPENING, None),
        counts=(previous.get("counts_file") or DEFAULT_COUNTS, None),
    )


def import_opening(settings: Settings, store, filename: str, raw: bytes) -> dict:
    previous = store.latest_run() or {}
    counts = (previous.get("counts_file") or DEFAULT_COUNTS, None)
    if previous:
        merged = store.run_input(previous["run_id"], "movements_merged")
        if merged is not None:
            # 上一批次是补丁批次：流水输入是合并集，不能按文件重读
            return run_chain(
                settings, store,
                movements=(previous.get("movements_file") or DEFAULT_MOVEMENTS, None),
                opening=(filename, raw), counts=counts,
                movement_records=patch_mod.records_from_canonical(merged),
            )
        stored = store.run_input(previous["run_id"], "movements")
        if stored is not None:
            return run_chain(
                settings, store,
                movements=(previous.get("movements_file") or DEFAULT_MOVEMENTS, stored),
                opening=(filename, raw), counts=counts,
            )
    return run_chain(
        settings, store,
        movements=(previous.get("movements_file") or DEFAULT_MOVEMENTS, None),
        opening=(filename, raw),
        counts=counts,
    )
