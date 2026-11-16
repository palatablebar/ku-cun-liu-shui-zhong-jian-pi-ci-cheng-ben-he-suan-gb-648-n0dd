"""增量补丁：变化行与当前基线按业务键合并，分类结果供复核。

取舍：上游每天只回传有变化的流水行时，不整批替换（未变化的旧行会丢记录），
也不等全量重导（当天核算停住），而是把补丁行与当前基线按 `(单据号, 行号)`
做版本裁决合并，合并集走同一条 质量 → 重放 → 成本 → 对账 链，当天即可核算。

基线携带两类行：

- 无解析码的采信行（含旧版本与冲突对），参与版本裁决；
- 有解析码的隔离行：它们从未入账，但也没解决——随基线携带留在当前账里，
  直到上游送来同业务键的可采信行（判 resolved 修复）才移出。

两条硬性质都落在合并算法上：

- 幂等：与基线同键、同版本、同指纹的行判 `unchanged` 直接丢弃，不进合并集；
  合并集按 canonical 全序排列后再算 sha 与 run_id，canonical 投影只含
  cells/标志/行号（不含随文件列序变化的 raw）。同一补丁重试时合并集逐字节
  相同，run_id 不变，流水行与 FIFO 层都不会新增。
- 可恢复：合并、分类、重放全部在内存算完，最后由一个事务写入（见
  `store.save_run`）；中途任何失败，旧批次与旧基线原样保留。
"""
from __future__ import annotations

import hashlib
import json

# 流水列的 canonical 顺序：与 config.MOVEMENT_ALIASES 的键序一致，
# 基线行重建 record 时按此顺序还原 raw 单元格。
CANONICAL_KEYS = (
    "doc_no", "line_no", "doc_type", "warehouse", "sku", "qty",
    "unit", "price", "ts", "direction", "ref_doc", "revision",
)

CATEGORIES = ("added", "upgraded", "conflict", "stale", "unchanged", "invalid")


def fingerprint_cells(cells: dict) -> str:
    """业务内容指纹：只认内部列名 → 原始单元格，与列顺序、文件无关。"""
    payload = json.dumps(sorted((cells or {}).items()), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def baseline_entries(records: list[dict], rows: list[dict]) -> list[dict]:
    """从一次质量构建结果提取基线：采信行与未解决的隔离行都随基线携带。

    完全相同（同键同版本同指纹）的行只留一份；隔离行记下首要原因码，
    重建时按 cells 与标志位复原，原因码在重跑时按同一套规则重算。
    """
    entries: dict[tuple, dict] = {}
    for record, row in zip(records, rows):
        fp = fingerprint_cells(record.get("cells"))
        key = (row["doc_no"], int(row["line_no"]), int(row["revision"]), fp)
        if key in entries:
            continue
        entries[key] = {
            "doc_no": row["doc_no"],
            "line_no": int(row["line_no"]),
            "revision": int(row["revision"]),
            "fingerprint": fp,
            "cells": {str(k): str(v) for k, v in (record.get("cells") or {}).items()},
            "row_no": int(record.get("row_no") or 0),
            "reason": (row["reason"] or "") if row["codes"] else "",
            "column_ok": bool(record.get("column_ok", True)),
            "truncated": bool(record.get("truncated")),
        }
    return list(entries.values())


def entry_to_record(entry: dict) -> dict:
    """基线条目 → 采信层 record；raw 按 canonical 列序还原，标志位保留。"""
    cells = {str(k): str(v) for k, v in (entry.get("cells") or {}).items()}
    return {
        "row_no": int(entry.get("row_no") or 0),
        "cells": cells,
        "raw": [cells.get(key, "") for key in CANONICAL_KEYS],
        "column_ok": bool(entry.get("column_ok", True)),
        "truncated": bool(entry.get("truncated")),
    }


def classify(baseline: list[dict], records: list[dict], norms: list[dict]) -> dict:
    """把补丁行分成 added / upgraded / conflict / stale / unchanged / invalid，
    并找出被本补丁修复的基线隔离行（resolved）。

    裁决口径与质量模块的版本裁决一致：同键最高版本内同指纹判重（unchanged，
    不进合并集）；同版本不同值判冲突；低版本判过旧。分类索引包含已分类的
    补丁行，补丁文件内部的重复行按同一口径处理。

    返回 carry：随基线带入合并集的条目（= 基线 − 被修复的隔离行）。
    """
    good_keys = {
        (norm["doc_no"], norm["line_no"]) for norm in norms if not norm["codes"]
    }
    resolved = [
        entry for entry in baseline
        if entry.get("reason") and (entry["doc_no"], entry["line_no"]) in good_keys
    ]
    resolved_ids = {id(entry) for entry in resolved}
    carry = [entry for entry in baseline if id(entry) not in resolved_ids]

    index: dict[tuple, dict[int, set]] = {}
    for entry in carry:
        key = (entry["doc_no"], int(entry["line_no"]))
        index.setdefault(key, {}).setdefault(int(entry["revision"]), set()).add(
            entry["fingerprint"])

    report: dict[str, list] = {name: [] for name in CATEGORIES}
    report["resolved"] = [
        {
            "doc_no": entry["doc_no"],
            "line_no": int(entry["line_no"]),
            "revision": int(entry["revision"]),
            "row_no": int(entry.get("row_no") or 0),
            "reason": entry.get("reason") or "",
        }
        for entry in resolved
    ]
    keep: list[tuple[dict, dict]] = []
    for record, norm in zip(records, norms):
        item = {
            "doc_no": norm["doc_no"],
            "line_no": norm["line_no"],
            "revision": int(norm["revision"]),
            "row_no": int(record.get("row_no") or 0),
        }
        fp = fingerprint_cells(record.get("cells"))
        key = (norm["doc_no"], norm["line_no"])
        revision = int(norm["revision"])
        known = index.get(key)
        if norm["codes"]:
            # 隔离行：已在基线里（同键同版本同指纹）判一致，否则带入合并集留证
            if known and any(fp in bag for bag in known.values()):
                report["unchanged"].append(item)
                continue
            item["reason"] = norm["codes"][0]
            report["invalid"].append(item)
            index.setdefault(key, {}).setdefault(revision, set()).add(fp)
            keep.append((record, norm))
            continue
        if known and any(fp in bag for bag in known.values()):
            report["unchanged"].append(item)
            continue
        if not known:
            report["added"].append(item)
        else:
            max_revision = max(known)
            if revision > max_revision:
                item["supersedes"] = max_revision
                report["upgraded"].append(item)
            elif revision == max_revision:
                report["conflict"].append(item)
            else:
                report["stale"].append(item)
        index.setdefault(key, {}).setdefault(revision, set()).add(fp)
        keep.append((record, norm))
    return {"report": report, "keep": keep, "carry": carry}


def sort_key(doc_no: str, line_no: int, revision: int, record: dict) -> tuple:
    """合并集的 canonical 全序：同一组行无论来自基线还是补丁，排完逐字节相同。"""
    cells = record.get("cells") or {}
    return (
        str(doc_no or ""),
        int(line_no or 0),
        int(revision or 0),
        fingerprint_cells(cells),
        int(record.get("row_no") or 0),
        json.dumps(sorted(cells.items()), ensure_ascii=False),
    )


def canonical_order(tagged: list[tuple]) -> list[dict]:
    """tagged: (record, doc_no, line_no, revision)；返回排序后的 record 列表。"""
    ordered = sorted(tagged, key=lambda item: sort_key(item[1], item[2], item[3], item[0]))
    return [item[0] for item in ordered]


def _projection(record: dict) -> dict:
    """canonical 投影：run_id 与重放存档只认这些字段。

    raw 随文件列序变化，不进投影；重建时按 canonical 列序还原。
    """
    return {
        "row_no": int(record.get("row_no") or 0),
        "cells": {
            str(k): str(v)
            for k, v in sorted((record.get("cells") or {}).items())
        },
        "column_ok": bool(record.get("column_ok", True)),
        "truncated": bool(record.get("truncated")),
    }


def canonical_bytes(records: list[dict]) -> bytes:
    """合并集的确定性字节形式：进 run_id，也作为该批次的重放输入存档。"""
    payload = [_projection(rec) for rec in records]
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def records_from_canonical(raw: bytes) -> list[dict]:
    """canonical 字节 → record 列表（重放时用，字段与采信层一致）。"""
    return [
        entry_to_record(
            {
                "row_no": item.get("row_no"),
                "cells": item.get("cells") or {},
                "column_ok": item.get("column_ok", True),
                "truncated": item.get("truncated"),
            }
        )
        for item in json.loads(raw.decode("utf-8"))
    ]


def baseline_digest(entries: list[dict]) -> str:
    """基线内容摘要：复核人可用它核对两边基线是否一致。"""
    payload = [
        [
            entry["doc_no"], int(entry["line_no"]), int(entry["revision"]),
            entry["fingerprint"], entry.get("reason") or "",
            1 if entry.get("column_ok", True) else 0,
            1 if entry.get("truncated") else 0,
            json.dumps(sorted((entry.get("cells") or {}).items()), ensure_ascii=False),
        ]
        for entry in sorted(
            entries,
            key=lambda e: (e["doc_no"], int(e["line_no"]), int(e["revision"]),
                           e["fingerprint"]),
        )
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def _verdicts(rows: list[dict]) -> dict:
    """doc_no → {(status, reason): 行数}，改判比较的输入。

    完全相同的重复行（duplicate）是文件卫生问题，不是业务判定，不参与比较。
    """
    out: dict[str, dict] = {}
    for row in rows:
        doc = row.get("doc_no") or ""
        if not doc or row.get("status") == "duplicate":
            continue
        bucket = out.setdefault(doc, {})
        key = (row.get("status") or "", row.get("reason") or "")
        bucket[key] = bucket.get(key, 0) + 1
    return out


def _verdict_list(bucket: dict) -> list[dict]:
    return [
        {"status": status, "reason": reason, "rows": count}
        for (status, reason), count in sorted(bucket.items())
    ]


def diff_doc_verdicts(old_rows: list[dict], new_rows: list[dict]) -> list[dict]:
    """对比两个批次的单据判定：列出因本次补丁而改判的单据。"""
    before = _verdicts(old_rows)
    after = _verdicts(new_rows)
    changed = []
    for doc in sorted(set(before) | set(after)):
        old_bucket = before.get(doc, {})
        new_bucket = after.get(doc, {})
        if old_bucket == new_bucket:
            continue
        changed.append(
            {
                "doc_no": doc,
                "before": _verdict_list(old_bucket),
                "after": _verdict_list(new_bucket),
            }
        )
    return changed
