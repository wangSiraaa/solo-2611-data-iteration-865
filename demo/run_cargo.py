"""货舱交班演示：彩色纸粒周转车的舱格身份、清洗证据与整批匹配。

运行::

    python3 -m demo.run_cargo          # 打印合成案例
    python3 -m demo.run_cargo --json   # 另写 demo/out/cargo_report.json

覆盖题面要求：
1. 舱格稳定身份：改号只改显示；重新内衬开始新的接触历史。
2. 已确认运输按票面组合与本次舱格记录放入颜色；空跑不新增颜色。
3. 清洗单逐道工序声明可清除颜色；只有同内衬、完成时间够晚的旧接触才消除。
4. 交班点剩余颜色及来源。
5. 每单分别有候选却整批无解 (W 要 A 或 B、N 只要 B，A 有蓝)。
6. 补一张覆盖蓝色的清洗单后 W→A、N→B 可行。
7. 同一格号落在另一挂车上的清洗单不构成证据。
8. 固定未洗状态、客户允许 W 接触蓝色时也可行。
9. 车场/订单两个库分别重开；跨班迟到清洗、并行抢同一格、日结后离线复查。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile

from weighbridge import (
    Catalog,
    CargoError,
    CellBusy,
    IN,
    LOAD,
    OUT,
    OrderBook,
    OrderBusy,
    Warehouse,
    Yard,
)


def build_confirmed_trip() -> tuple[Warehouse, str]:
    c = Catalog()
    c.define_mass("tractor", "T-COLOR", 7050, 7150)
    c.define_mass("trailer", "R-COLOR", 1550, 1650)
    c.apply_attach("T-COLOR", "R-COLOR")  # seq 1
    w = Warehouse(c)
    w.add_ticket(dict(ticket_id="IN-C", tractor="T-COLOR", catalog_seq=1,
                      ts=1000, direction=IN, gross=9000))
    w.add_ticket(dict(ticket_id="OUT-C", tractor="T-COLOR", catalog_seq=1,
                      ts=1200, direction=OUT, gross=12500))
    ev = w.preview(LOAD, "IN-C", "OUT-C")
    w.confirm(ev.trip_id)
    return w, ev.trip_id


def report() -> dict:
    out: dict = {}
    w, trip = build_confirmed_trip()

    # ---- 车场：一只挂车上两个舱格 A、B（稳定身份 CELL0001/CELL0002）
    y = Yard(warehouse=w)
    A = y.add_cell("R-COLOR", "A", ts=0)
    B = y.add_cell("R-COLOR", "B", ts=0)
    out["cells"] = {"A": A, "B": B}

    # 已确认运输把蓝色纸粒放入 A 格 (票面组合 -> 挂车 R-COLOR -> 舱格 A)
    y.load_from_trip(trip, "blue", cell_id=A)
    # 一次空跑：不新增颜色
    y.load_cargo(B, None, ts=1100)

    # 改号只改显示 (A -> A1)，接触史不变
    y.rename(A, "A1", ts=1150)
    out["rename"] = {
        "label_at_1140": y.label_at(A, 1140),
        "label_at_1160": y.label_at(A, 1160),
        "residual_A_unaffected": sorted(y.residual_color_set(A, 1300)),
    }
    out["handover_unwashed"] = y.handover(1300)

    # ---- 订单：客户白单 W 禁蓝、可用 A 或 B；普通单 N 不限色但只能 B
    ob = OrderBook(yard=y)
    ob.set_ban("white", "white", ["blue"])
    ob.add_order("W", "white", "white", point=1300, allowed_labels=["A1", "B"])
    ob.add_order("N", "normal", "normal", point=1300, allowed_labels=["B"])

    per_order = ob.candidates(1300)
    out["per_order_candidates"] = {
        oid: per_order[oid]["eligible"] for oid in per_order
    }
    sol_before = ob.solve_point(1300)
    out["batch_before_cleaning"] = {
        "feasible": sol_before["feasible"],
        "impediments": sol_before["impediments"],
        "note": "每单各自都有候选，但两单都只能落在 B，整批无解 (Hall 阻碍)",
    }

    # ---- 补一张覆盖蓝色的本内衬清洗单 (逐道工序)
    slip = y.clean(A, done_ts=1250, steps=[
        {"step": "干刷", "clears": []},
        {"step": "蓝专用溶剂冲洗", "clears": ["blue"]},
    ])
    out["cleaning_slip"] = slip
    out["handover_washed"] = y.handover(1300)

    sol_after = ob.solve_point(1300)
    out["batch_after_cleaning"] = {
        "feasible": sol_after["feasible"],
        "matching": sol_after["matching"],
    }

    # ---- 预审 -> 正式接受 (只剩这一份被接受)
    p_ok = ob.preview(["W", "N"], point=1300)
    p_alt = ob.preview(["N"], point=1300)
    accepted = ob.accept(p_ok["arr_id"])
    try:
        ob.accept(p_alt["arr_id"])
        alt = "accepted unexpectedly"
    except (CellBusy, OrderBusy) as exc:
        alt = f"blocked: {type(exc).__name__}: {exc}"
    out["arrangements"] = {
        "preview_ok": p_ok["mapping"],
        "preview_alt": p_alt["mapping"],
        "accepted": accepted["mapping"],
        "alt_accept": alt,
        "alt_status": ob.arrangement(p_alt["arr_id"])["status"],
    }

    # ---- 同车牌另一挂车、收据同样写 "A1" 的清洗证据不覆盖本舱格
    y_other = Yard()
    a_r1 = y_other.add_cell("R-COLOR", "A1", ts=0)
    a_r2 = y_other.add_cell("R-OTHER", "A1", ts=0)
    y_other.load_cargo(a_r1, "blue", ts=10)
    try:
        y_other.clean(a_r1, done_ts=20, trailer="R-OTHER", label_seen="A1",
                      steps=[{"step": "flush", "clears": ["blue"]}])
        cross = "unexpectedly accepted"
    except CargoError as exc:
        cross = f"rejected: {exc}"
    out["cross_trailer_evidence"] = {
        "clean_on_target": cross,
        "target_residual": sorted(y_other.residual_color_set(a_r1, 100)),
        "note": "同格号落在另一挂车的舱格上，不构成对本舱格的清洗证据",
    }

    # ---- 固定未洗状态，客户允许 W 接触蓝色时也能履行
    y_allow = Yard()
    a2 = y_allow.add_cell("R1", "A", ts=0)
    b2 = y_allow.add_cell("R1", "B", ts=0)
    y_allow.load_cargo(a2, "blue", ts=10)
    ob_allow = OrderBook(yard=y_allow)
    ob_allow.set_ban("white", "white", [])  # 该版本不禁蓝
    ob_allow.add_order("W", "white", "white", point=100, allowed_labels=["A", "B"])
    ob_allow.add_order("N", "normal", "normal", point=100, allowed_labels=["B"])
    out["allow_blue_variant"] = ob_allow.solve_point(100)

    # ---- 跨班迟到清洗单：交班点 100 之前完成、之后才登记
    y_late = Yard()
    a3 = y_late.add_cell("R1", "A", ts=0)
    y_late.load_cargo(a3, "blue", ts=10)
    ob_late = OrderBook(yard=y_late)
    ob_late.set_ban("v", "white", ["blue"])
    ob_late.add_order("W", "white", "v", point=100, allowed_labels=["A"])
    before = ob_late.solve_point(100)["feasible"]
    y_late.clean(a3, done_ts=50, steps=[{"step": "flush", "clears": ["blue"]}])
    after = ob_late.solve_point(100)["feasible"]
    out["late_cleaning"] = {
        "feasible_before_slip": before,
        "residual_at_40": sorted(y_late.residual_color_set(a3, 40)),
        "feasible_after_slip": after,
        "note": "完成时间 50 早于交班点 100，迟到登记后覆盖生效；40 处仍见蓝色",
    }

    out["reconcile"] = {
        "yard": y.reconcile(),
        "orders": ob.reconcile(),
        "frozen_recheck": ob.recheck(accepted["arr_id"])["accepted_ok"],
    }
    return out


def offline_report() -> dict:
    """两库分别落盘、分别重开、日结后离线复查。"""
    d = tempfile.mkdtemp()
    yp = pathlib.Path(d) / "yard.db"
    op = pathlib.Path(d) / "order.db"
    y = Yard(path=str(yp))
    A = y.add_cell("R1", "A", ts=0)
    B = y.add_cell("R1", "B", ts=0)
    y.load_cargo(A, "blue", ts=10)
    ob = OrderBook(path=str(op), yard=y)
    ob.set_ban("white", "white", ["blue"])
    ob.add_order("W", "white", "white", point=100, allowed_labels=["A", "B"])
    ob.add_order("N", "normal", "normal", point=100, allowed_labels=["B"])
    y.clean(A, done_ts=50, steps=[{"step": "flush", "clears": ["blue"]}])
    acc = ob.accept(ob.preview(["W", "N"], point=100)["arr_id"])
    y.close()
    ob.close()

    y2 = Yard(path=str(yp))
    ob2 = OrderBook(path=str(op))
    result = {
        "accepted_mapping": acc["mapping"],
        "yard_reconcile_after_reopen": y2.reconcile(),
        "order_reconcile_after_reopen": ob2.reconcile(),
        "frozen_recheck": ob2.recheck(acc["arr_id"])["accepted_ok"],
    }
    y2.close()
    ob2.close()
    # 订单侧单独重开、完全不连车场也能复查 (只用冻结证据)
    ob_offline = OrderBook(path=str(op))
    result["offline_recheck_without_yard"] = ob_offline.recheck(acc["arr_id"])["accepted_ok"]
    ob_offline.close()
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="写出 demo/out/cargo_report.json")
    args = ap.parse_args()

    data = {"online": report(), "offline": offline_report()}
    print(json.dumps(data, indent=2, ensure_ascii=False))

    if args.json:
        out = pathlib.Path(__file__).parent / "out"
        out.mkdir(exist_ok=True)
        (out / "cargo_report.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nwrote {out}/cargo_report.json", flush=True)


if __name__ == "__main__":
    main()
