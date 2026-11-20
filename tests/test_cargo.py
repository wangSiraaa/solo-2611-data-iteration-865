"""货舱交班子系统测试：舱格身份 / 内衬 / 清洗证据 / 匹配 / 冻结复查。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import unittest

from weighbridge import (
    Catalog,
    CargoError,
    CellBusy,
    IN,
    LOAD,
    OUT,
    OrderBook,
    OrderBusy,
    STATUS_ACCEPTED,
    STATUS_DRAFT,
    Warehouse,
    Yard,
)
from weighbridge.cargo import ArrangementRejected, _max_matching


def make_warehouse() -> tuple[Catalog, Warehouse, str]:
    """搭一台已确认的换挂运输，返回 (catalog, warehouse, confirmed_trip_id)。"""
    c = Catalog()
    c.define_mass("tractor", "T1", 7000, 7200)
    c.define_mass("trailer", "R1", 1500, 1600)
    c.apply_attach("T1", "R1")
    w = Warehouse(c)
    w.add_ticket(dict(ticket_id="K1", tractor="T1", catalog_seq=1,
                      ts=100, direction=IN, gross=9000))
    w.add_ticket(dict(ticket_id="K2", tractor="T1", catalog_seq=1,
                      ts=200, direction=OUT, gross=12000))
    ev = w.preview(LOAD, "K1", "K2")
    w.confirm(ev.trip_id)
    return c, w, ev.trip_id


class CellIdentityTests(unittest.TestCase):
    def test_rename_changes_display_only(self):
        y = Yard()
        cid = y.add_cell("R1", "A", ts=0)
        y.load_cargo(cid, "blue", ts=10)
        y.rename(cid, "Z", ts=20)
        self.assertEqual(y.label_at(cid, 15), "A")
        self.assertEqual(y.label_at(cid, 25), "Z")
        self.assertEqual(y.residual_color_set(cid, 100), {"blue"})
        self.assertEqual(y.reconcile(), [])

    def test_reline_starts_new_contact_history(self):
        y = Yard()
        cid = y.add_cell("R1", "A", ts=0)
        y.load_cargo(cid, "blue", ts=10)
        self.assertEqual(y.residual_color_set(cid, 25), {"blue"})
        y.reline(cid, ts=30)
        # 新内衬上没有任何接触
        self.assertEqual(y.residual_color_set(cid, 40), set())
        # 旧内衬证据仍在历史中 (在重内衬之前的交班点可见)
        self.assertEqual(y.residual_color_set(cid, 20), {"blue"})
        y.load_cargo(cid, "red", ts=50)
        self.assertEqual(y.residual_color_set(cid, 60), {"red"})

    def test_old_liner_cleaning_does_not_clear_new_liner(self):
        y = Yard()
        cid = y.add_cell("R1", "A", ts=0)
        y.load_cargo(cid, "red", ts=50)  # on L0
        # Hmm: reline after
        y.reline(cid, ts=30)
        y.load_cargo(cid, "red", ts=50)  # now on L1
        # Cleaning from L0 (registered before reline) must not clear L1 red.
        y.clean(cid, done_ts=25, steps=[{"step": "flush", "clears": ["red"]}])
        self.assertIn("red", y.residual_color_set(cid, 60))

    def test_label_resolution_requires_unique_identity(self):
        y = Yard()
        a1 = y.add_cell("R1", "A", ts=0)
        # 不同挂车可以有同名格号
        y.add_cell("R2", "A", ts=0)
        self.assertEqual(y.cell_by_label("R1", "A", 100), a1)
        with self.assertRaises(CargoError):
            y.cell_by_label("R1", "B", 100)

    def test_duplicate_label_on_same_trailer_rejected(self):
        y = Yard()
        y.add_cell("R1", "A", ts=0)
        with self.assertRaises(CargoError):
            y.add_cell("R1", "A", ts=5)


class CleaningEvidenceTests(unittest.TestCase):
    def test_step_wise_coverage_only_removes_permitted_colors(self):
        y = Yard()
        cid = y.add_cell("R1", "A", ts=0)
        y.load_cargo(cid, "blue", ts=10)
        y.load_cargo(cid, "red", ts=20)
        # Only one chemical clears blue, not red.
        y.clean(cid, done_ts=30, steps=[{"step": "blue-only", "clears": ["blue"]}])
        self.assertEqual(y.residual_color_set(cid, 40), {"red"})

    def test_cleaning_cannot_cover_earlier_later_contacts(self):
        y = Yard()
        cid = y.add_cell("R1", "A", ts=0)
        y.load_cargo(cid, "blue", ts=10)
        y.load_cargo(cid, "blue", ts=50)
        # Completed at 30: clears the 10 contact but not the 50 contact.
        y.clean(cid, done_ts=30, steps=[{"step": "flush", "clears": ["blue"]}])
        residual = y.residual_colors(cid, 60)
        self.assertEqual([e["color"] for e in residual], ["blue"])
        self.assertEqual(residual[0]["since"], 50)

    def test_same_label_other_trailer_is_not_evidence(self):
        y = Yard()
        r1a = y.add_cell("R1", "A", ts=0)
        r2a = y.add_cell("R2", "A", ts=0)
        y.load_cargo(r1a, "blue", ts=10)
        # Receipt says "A" but actually served R2's cell.
        with self.assertRaises(CargoError):
            y.clean(r1a, done_ts=30, trailer="R2", label_seen="A",
                    steps=[{"step": "flush", "clears": ["blue"]}])
        self.assertEqual(y.residual_color_set(r1a, 40), {"blue"})
        # Registering it against R2 is fine, but leaves R1 untouched.
        y.clean(r2a, done_ts=30, trailer="R2", label_seen="A",
                steps=[{"step": "flush", "clears": ["blue"]}])
        self.assertEqual(y.residual_color_set(r1a, 40), {"blue"})

    def test_explicit_cell_hint_blocks_misdirected_evidence(self):
        y = Yard()
        r1a = y.add_cell("R1", "A", ts=0)
        r2a = y.add_cell("R2", "A", ts=0)
        y.load_cargo(r1a, "blue", ts=10)
        with self.assertRaises(CargoError):
            y.clean(r1a, done_ts=30, steps=[{"step": "flush", "clears": ["blue"]}],
                    trailer="R2", cell_hint=r2a)
        self.assertIn("blue", y.residual_color_set(r1a, 40))

    def test_empty_run_adds_no_color(self):
        y = Yard()
        cid = y.add_cell("R1", "A", ts=0)
        y.load_cargo(cid, None, ts=10)
        self.assertEqual(y.residual_color_set(cid, 20), set())
        self.assertEqual(y.conn.execute("SELECT COUNT(*) AS n FROM contact")
                         .fetchone()["n"], 0)

    def test_drained_color_stays_in_history_until_cleaned(self):
        y = Yard()
        cid = y.add_cell("R1", "A", ts=0)
        y.load_cargo(cid, "blue", ts=10)
        y.load_cargo(cid, None, ts=20)  # 排空
        self.assertIn("blue", y.residual_color_set(cid, 30))


class FromTripTests(unittest.TestCase):
    def test_only_confirmed_trips_place_cargo(self):
        c = Catalog()
        c.define_mass("tractor", "T1", 7000, 7200)
        c.define_mass("trailer", "R1", 1500, 1600)
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(dict(ticket_id="K1", tractor="T1", catalog_seq=1,
                          ts=100, direction=IN, gross=9000))
        w.add_ticket(dict(ticket_id="K2", tractor="T1", catalog_seq=1,
                          ts=200, direction=OUT, gross=12000))
        ev = w.preview(LOAD, "K1", "K2")
        y = Yard(warehouse=w)
        A = y.add_cell("R1", "A", ts=0)
        B = y.add_cell("R1", "B", ts=0)
        with self.assertRaises(CargoError):
            y.load_from_trip(ev.trip_id, "blue", cell_id=A)
        w.confirm(ev.trip_id)
        rec = y.load_from_trip(ev.trip_id, "blue", cell_id=A)
        self.assertEqual(rec["cell_id"], A)
        self.assertEqual(rec["trailer"], "R1")
        self.assertEqual(y.residual_color_set(A, 300), {"blue"})
        self.assertEqual(y.residual_color_set(B, 300), set())

    def test_requires_explicit_cell_when_trailer_has_two(self):
        _, w, trip = make_warehouse()
        y = Yard(warehouse=w)
        y.add_cell("R1", "A", ts=0)
        y.add_cell("R1", "B", ts=0)
        with self.assertRaises(CargoError):
            y.load_from_trip(trip, "blue")

    def test_empty_run_from_trip_adds_no_color(self):
        _, w, trip = make_warehouse()
        y = Yard(warehouse=w)
        A = y.add_cell("R1", "A", ts=0)
        rec = y.load_from_trip(trip, None, cell_id=A)
        self.assertIsNone(rec["color"])
        self.assertEqual(y.residual_color_set(A, 300), set())
        self.assertEqual(
            y.conn.execute("SELECT COUNT(*) AS n FROM contact").fetchone()["n"], 0
        )

    def test_idempotent_and_conflicting_trip_records(self):
        _, w, trip = make_warehouse()
        y = Yard(warehouse=w)
        A = y.add_cell("R1", "A", ts=0)
        r1 = y.load_from_trip(trip, "blue", cell_id=A)
        self.assertEqual(y.load_from_trip(trip, "blue", cell_id=A), r1)
        with self.assertRaises(CargoError):
            y.load_from_trip(trip, "red", cell_id=A)


class BatchMatchingTests(unittest.TestCase):
    def _two_cell_yard(self):
        y = Yard()
        A = y.add_cell("R1", "A", ts=0)
        B = y.add_cell("R1", "B", ts=0)
        return y, A, B

    def _white_normal(self):
        ob_yard_holder = {}

        def build(y):
            ob = OrderBook(yard=y)
            ob.set_ban("white", "white", ["blue"])
            ob.add_order("W", "white", "white", point=100, allowed_labels=["A", "B"])
            ob.add_order("N", "normal", "normal", point=100, allowed_labels=["B"])
            return ob
        return build

    def test_per_order_candidates_mislead_batch(self):
        y, A, B = self._two_cell_yard()
        y.load_cargo(A, "blue", ts=10)  # B 无色
        ob = self._white_normal()(y)
        cand = ob.candidates(100)
        # 每单各自都有候选……
        self.assertEqual(cand["W"]["eligible"], [B])
        self.assertEqual(cand["N"]["eligible"], [B])
        # ……但整批不可行：两单都只想要 B。
        sol = ob.solve_point(100)
        self.assertFalse(sol["feasible"])
        self.assertIn("impediments", sol)
        self.assertIn("N", sol["impediments"]["hall_orders"])
        self.assertIn("W", sol["impediments"]["hall_orders"])
        causes = {d["order_id"]: d["cause"] for d in sol["impediments"]["detail"]}
        self.assertEqual(causes["N"], "CONTENTION")

    def test_cleaning_blue_on_A_makes_batch_feasible(self):
        y, A, B = self._two_cell_yard()
        y.load_cargo(A, "blue", ts=10)
        ob = self._white_normal()(y)
        y.clean(A, done_ts=50, steps=[{"step": "flush", "clears": ["blue"]}])
        sol = ob.solve_point(100)
        self.assertTrue(sol["feasible"])
        self.assertEqual(sol["matching"], {"N": B, "W": A})

    def test_fixed_unwashed_but_customer_allows_blue(self):
        y, A, B = self._two_cell_yard()
        y.load_cargo(A, "blue", ts=10)
        ob = OrderBook(yard=y)
        # 客户版本允许 W 接触蓝色 (禁配集合为空)
        ob.set_ban("white", "white", [])
        ob.add_order("W", "white", "white", point=100, allowed_labels=["A", "B"])
        ob.add_order("N", "normal", "normal", point=100, allowed_labels=["B"])
        sol = ob.solve_point(100)
        self.assertTrue(sol["feasible"])
        self.assertEqual(sol["matching"], {"N": B, "W": A})

    def test_allowed_labels_restriction(self):
        y, A, B = self._two_cell_yard()
        ob = OrderBook(yard=y)
        ob.add_order("X", "red", "v", point=100, allowed_labels=["A"])
        ob.add_order("Y", "red", "v", point=100, allowed_labels=["A"])
        sol = ob.solve_point(100)
        self.assertFalse(sol["feasible"])  # 两单都只许 A，一格不能给两单

    def test_hall_violator_shape(self):
        # S = {o1,o2,o3} all only admit c1 -> N(S) = {c1}
        edges = {"o1": ["c1"], "o2": ["c1"], "o3": ["c1"]}
        match_order, match_cell = _max_matching(list(edges), edges)
        from weighbridge.cargo import _hall_violator
        s, n = _hall_violator(list(edges), edges, match_order, match_cell)
        self.assertEqual(len(s), 3)
        self.assertEqual(n, ["c1"])
        self.assertGreater(len(s), len(n))

    def test_no_eligible_cell_cause(self):
        y = Yard()
        A = y.add_cell("R1", "A", ts=0)
        y.load_cargo(A, "blue", ts=10)
        ob = OrderBook(yard=y)
        ob.set_ban("v", "white", ["blue"])
        ob.add_order("W", "white", "v", point=100, allowed_labels=["A"])
        sol = ob.solve_point(100)
        self.assertFalse(sol["feasible"])
        detail = sol["impediments"]["detail"][0]
        self.assertEqual(detail["cause"], "NO_ELIGIBLE_CELL")
        self.assertIn("CELL0001", detail["blocked"])


class ArrangementLifecycleTests(unittest.TestCase):
    def _setup(self):
        y = Yard()
        A = y.add_cell("R1", "A", ts=0)
        B = y.add_cell("R1", "B", ts=0)
        y.load_cargo(A, "blue", ts=10)
        ob = OrderBook(yard=y)
        ob.set_ban("white", "white", ["blue"])
        ob.add_order("W", "white", "white", point=100, allowed_labels=["A", "B"])
        ob.add_order("N", "normal", "normal", point=100, allowed_labels=["B"])
        return y, ob, A, B

    def test_preview_does_not_reserve(self):
        y, ob, A, B = self._setup()
        y.clean(A, done_ts=50, steps=[{"step": "flush", "clears": ["blue"]}])
        p1 = ob.preview(["W", "N"], point=100)
        self.assertEqual(p1["status"], STATUS_DRAFT)
        self.assertEqual(ob.occupancy(p1["arr_id"]), [])

    def test_multiple_previews_then_accept_one(self):
        y, ob, A, B = self._setup()
        y.clean(A, done_ts=50, steps=[{"step": "flush", "clears": ["blue"]}])
        p1 = ob.preview(["W", "N"], point=100)
        p2 = ob.preview(["W"], point=100)
        # 接受一份；另一份仍可预览，但共享 W 的占用会阻止其接受。
        acc = ob.accept(p1["arr_id"])
        self.assertEqual(acc["status"], STATUS_ACCEPTED)
        with self.assertRaises((CellBusy, OrderBusy)):
            ob.accept(p2["arr_id"])
        self.assertEqual(ob.arrangement(p2["arr_id"])["status"], STATUS_DRAFT)
        self.assertEqual(ob.reconcile(), [])

    def test_parallel_previews_fighting_same_cell(self):
        y = Yard()
        A = y.add_cell("R1", "A", ts=0)
        ob = OrderBook(yard=y)
        ob.add_order("O1", "red", "v", point=100)
        ob.add_order("O2", "red", "v", point=100)
        p1 = ob.preview(["O1"], point=100)
        p2 = ob.preview(["O2"], point=100)
        self.assertEqual(list(p1["mapping"].values()), list(p2["mapping"].values()))
        ob.accept(p1["arr_id"])
        with self.assertRaises(CellBusy):
            ob.accept(p2["arr_id"])
        self.assertEqual(ob.arrangement(p2["arr_id"])["status"], STATUS_DRAFT)

    def test_same_cell_reusable_at_different_points(self):
        y = Yard()
        A = y.add_cell("R1", "A", ts=0)
        y.load_cargo(A, "blue", ts=10)
        ob = OrderBook(yard=y)
        # blue remains; but point 200 uses a cleaning after it
        y.clean(A, done_ts=150, steps=[{"step": "flush", "clears": ["blue"]}])
        ob.set_ban("v", "white", ["blue"])
        ob.add_order("P1", "white", "v", point=20, allowed_labels=["A"])  # at 20 blue present
        ob.add_order("P2", "white", "v", point=200, allowed_labels=["A"])  # cleaned by 150
        # P1 at point 20 sees blue -> infeasible; use non-banned color instead
        ob.add_order("P3", "grey", "v", point=20, allowed_labels=["A"])
        a = ob.accept(ob.preview(["P3"], point=20)["arr_id"])
        b = ob.accept(ob.preview(["P2"], point=200)["arr_id"])
        self.assertEqual(a["mapping"]["P3"], A)
        self.assertEqual(b["mapping"]["P2"], A)
        self.assertEqual(ob.reconcile(), [])

    def test_accept_revalidates_and_rejects_stale_arrangement(self):
        y, ob, A, B = self._setup()
        # Preview while infeasible (blue not yet cleaned)
        p = ob.preview(["W", "N"], point=100)
        with self.assertRaises(ArrangementRejected):
            ob.accept(p["arr_id"])

    def test_recheck_uses_frozen_version_and_cleaning_evidence(self):
        y, ob, A, B = self._setup()
        y.clean(A, done_ts=50, steps=[{"step": "flush", "clears": ["blue"]}])
        acc = ob.accept(ob.preview(["W", "N"], point=100)["arr_id"])
        recheck = ob.recheck(acc["arr_id"])
        self.assertTrue(recheck["accepted_ok"])
        # 后来给同一版本追加蓝色禁配，不得改变冻结复查。
        ob.set_ban("white", "white", ["blue", "red"])
        self.assertTrue(ob.recheck(acc["arr_id"])["accepted_ok"])
        # 原清洗凭证被列出
        slips = recheck["orders"]["W"]["cleaning_slips"]
        self.assertEqual(len(slips), 1)
        self.assertEqual(slips[0]["covered"], ["blue"])

    def test_frozen_recheck_survives_reline_and_new_contact(self):
        y, ob, A, B = self._setup()
        y.clean(A, done_ts=50, steps=[{"step": "flush", "clears": ["blue"]}])
        acc = ob.accept(ob.preview(["W", "N"], point=100)["arr_id"])
        # 迟到补录：在交班点 100 之前 (ts=90) 实际还接触过蓝色，此刻才补登记。
        y.load_cargo(A, "blue", ts=90)
        # 冻结复查按原凭证，不受迟到记录影响。
        self.assertTrue(ob.recheck(acc["arr_id"])["accepted_ok"])
        # 但按当前车场状态，交班点 100 已出现未清除的蓝色 -> 不再合格。
        self.assertFalse(ob.live_status(acc["arr_id"])["still_ok"])

    def test_late_cleaning_slip_crosses_shift_boundary(self):
        y = Yard()
        A = y.add_cell("R1", "A", ts=0)
        y.load_cargo(A, "blue", ts=10)
        ob = OrderBook(yard=y)
        ob.set_ban("v", "white", ["blue"])
        ob.add_order("W", "white", "v", point=100, allowed_labels=["A"])
        # 交班点 100 之前没有清洗证据 -> 不可行
        self.assertFalse(ob.solve_point(100)["feasible"])
        # 迟到清洗单：完成时间 50 (早于交班点)，此刻才登记
        y.clean(A, done_ts=50, steps=[{"step": "flush", "clears": ["blue"]}])
        self.assertTrue(ob.solve_point(100)["feasible"])
        # 但在完成时间之前(交班点 40) 仍看得见蓝色
        self.assertIn("blue", y.residual_color_set(A, 40))


class PersistenceTests(unittest.TestCase):
    def test_reopen_yard_and_order_db_separately(self):
        d = tempfile.mkdtemp()
        yp = os.path.join(d, "yard.db")
        op = os.path.join(d, "order.db")
        y = Yard(path=yp)
        A = y.add_cell("R1", "A", ts=0)
        B = y.add_cell("R1", "B", ts=0)
        y.load_cargo(A, "blue", ts=10)
        ob = OrderBook(path=op, yard=y)
        ob.set_ban("white", "white", ["blue"])
        ob.add_order("W", "white", "white", point=100, allowed_labels=["A", "B"])
        ob.add_order("N", "normal", "normal", point=100, allowed_labels=["B"])
        y.clean(A, done_ts=50, steps=[{"step": "flush", "clears": ["blue"]}])
        acc = ob.accept(ob.preview(["W", "N"], point=100)["arr_id"])
        y.close()
        ob.close()

        # 分别重开；订单侧独立于车场连接
        y2 = Yard(path=yp)
        ob2 = OrderBook(path=op)
        self.assertEqual(y2.reconcile(), [])
        self.assertEqual(ob2.reconcile(), [])
        self.assertTrue(ob2.recheck(acc["arr_id"])["accepted_ok"])
        self.assertEqual(y2.residual_color_set(A, 100), set())
        y2.close()
        ob2.close()

    def test_offline_recheck_after_end_of_day(self):
        d = tempfile.mkdtemp()
        yp = os.path.join(d, "yard.db")
        op = os.path.join(d, "order.db")
        y = Yard(path=yp)
        A = y.add_cell("R1", "A", ts=0)
        y.load_cargo(A, "blue", ts=10)
        ob = OrderBook(path=op, yard=y)
        ob.set_ban("v", "white", ["blue"])
        ob.add_order("W", "white", "v", point=100, allowed_labels=["A"])
        y.clean(A, done_ts=50, steps=[{"step": "flush", "clears": ["blue"]}])
        acc = ob.accept(ob.preview(["W"], point=100)["arr_id"])
        y.close()
        ob.close()
        # 次日离线复查：只用订单库与冻结证据，不需要车场连接
        ob_offline = OrderBook(path=op)
        rc = ob_offline.recheck(acc["arr_id"])
        self.assertTrue(rc["accepted_ok"])
        self.assertEqual(rc["orders"]["W"]["label"], "A")
        ob_offline.close()


if __name__ == "__main__":
    unittest.main()
