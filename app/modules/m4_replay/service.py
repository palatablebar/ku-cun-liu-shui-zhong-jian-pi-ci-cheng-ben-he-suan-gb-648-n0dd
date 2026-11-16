"""重放编排：从批次输入重算层与消耗段，并写回本批。

单独重跑（POST /api/replay）优先读本批登记的输入存档（整批导入的原始字节、
补丁批次的合并集 canonical 字节），用同一套规则重算，行数与合计与首跑一致；
老批次没有存档时回退到样例目录里登记的文件。
"""
from __future__ import annotations

from pathlib import Path

from ...errors import DomainError
from ..m1_ingest import patch as patch_mod
from ..m1_ingest import service as m1_service
from ..m2_doc_rules import contract
from ..m2_doc_rules import service as m2_service
from . import replay as replay_mod


def replay_rows(settings, rows: list[dict], opening_rows: list[dict]) -> dict:
    return replay_mod.replay(rows, opening_rows, settings)


def load_opening(settings, filename: str) -> list[dict]:
    if not filename:
        return []
    base = Path(settings.sample_dir).resolve()
    path = (base / filename).resolve()
    if base not in path.parents and path.parent != base:
        raise DomainError("E_REPLAY_INPUT", "期初文件名不合法", 400)
    if not path.is_file():
        raise DomainError("E_REPLAY_INPUT", f"期初文件已不在样例目录：{filename}", 409)
    parsed = m2_service.read_opening(settings, filename, path.read_bytes())
    return parsed["rows"]


def _movement_rows(settings, store, run) -> list[dict]:
    """本批流水行：合并集存档 → 原始字节存档 → 样例目录文件。"""
    from ..m3_quality import service as m3_service

    run_id = run["run_id"]
    merged = store.run_input(run_id, "movements_merged")
    if merged is not None:
        parsed = {
            "records": patch_mod.records_from_canonical(merged),
            "state": None,
            "blank_rows": 0,
        }
        return m3_service.build(settings, parsed)["rows"]
    stored = store.run_input(run_id, "movements")
    if stored is not None:
        parsed = m1_service.read_file(
            settings, run.get("movements_file") or "movements.csv", stored,
            contract.aliases("movements"))
        return m3_service.build(settings, parsed)["rows"]

    movements_file = run.get("movements_file") or ""
    base = Path(settings.sample_dir).resolve()
    path = (base / movements_file).resolve()
    if base not in path.parents and path.parent != base:
        raise DomainError("E_REPLAY_INPUT", "流水文件名不合法", 400)
    if not path.is_file():
        raise DomainError("E_REPLAY_INPUT", f"流水文件已不在样例目录：{movements_file}", 409)
    parsed = m1_service.read_file(
        settings, movements_file, path.read_bytes(), contract.aliases("movements"))
    return m3_service.build(settings, parsed)["rows"]


def _opening_rows(settings, store, run) -> list[dict]:
    stored = store.run_input(run["run_id"], "opening")
    if stored is not None:
        return m2_service.read_opening(
            settings, run.get("opening_file") or "opening.csv", stored)["rows"]
    return load_opening(settings, run.get("opening_file") or "")


def apply(settings, store, run) -> dict:
    """单独重跑：读本批登记的输入，重算并替换层、消耗段与行状态。"""
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 409)
    rows = _movement_rows(settings, store, run)
    opening_rows = _opening_rows(settings, store, run)
    outcome = replay_rows(settings, rows, opening_rows)
    store.replace_replay(run["run_id"], outcome["layers"], outcome["segments"], rows)
    store.update_totals(
        run["run_id"],
        {
            "opening_qty": outcome["totals"]["opening_qty"],
            "in_qty": outcome["totals"]["in_qty"],
            "out_qty": outcome["totals"]["out_qty"],
            "adj_in_qty": outcome["totals"]["adj_in_qty"],
            "adj_out_qty": outcome["totals"]["adj_out_qty"],
            "ending_qty": outcome["totals"]["ending_qty"],
            "cogs": outcome["totals"]["cogs"],
            "ending_value": outcome["totals"]["ending_value"],
        },
    )
    return summary(outcome, run["run_id"])


def summary(outcome: dict, run_id: str) -> dict:
    totals = outcome["totals"]
    return {
        "run_id": run_id,
        "pairs": outcome["pairs"],
        "broken_transfers": outcome["broken_transfers"],
        "layers": len(outcome["layers"]),
        "segments": len(outcome["segments"]),
        "applied": totals["applied"],
        "quantities": {
            key: format(totals[key], "f")
            for key in ("opening_qty", "in_qty", "out_qty", "adj_in_qty",
                        "adj_out_qty", "ending_qty")
        },
        "cogs": format(totals["cogs"], "f"),
        "ending_value": format(totals["ending_value"], "f"),
    }
