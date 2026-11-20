"""货舱交班 (cargo-hold handover) —— 彩色纸粒周转车。

本模块给过磅项目补上一层 "货舱" 事实：

* **车场侧** :class:`Yard` —— 为挂车登记具有稳定身份的舱格 (cell)，记录重新
  内衬 (reline)、改号 (rename)、已确认运输放入的纸粒颜色、以及清洗单
  (cleaning slip)。它只从 :class:`weighbridge.warehouse.Warehouse` 的 *已确认*
  运输取得票面组合与挂车，绝不凭空猜挂车。
* **订单侧** :class:`OrderBook` —— 订货方的客户版本、按 "拟装颜色 + 客户版本"
  登记的禁配集合、订单批次与安排 (arrangement)。一批订单必须在同一交班点分给
  互不相同的合格舱格；能履行时给出一组匹配，无解时列明 Hall 阻碍。

两侧各用自己的 SQLite 库，可分别关闭、重开、离线复查。

关键语义
--------
* 舱格 ``cell_id`` 是稳定身份；改号只改显示，接触史不随之改变。重新内衬会创建
  新的 ``liner``，新内衬的接触史从空开始；旧内衬的接触仍在历史里。
* 接触 (contact) 只来自 *已确认* 运输；空跑 (color=None) 不新增颜色。已排空
  的颜色仍留在接触史，只有清洗证据才把它从 "剩余颜色" 中移除。
* 清洗单记录完成时间、当时内衬和逐道工序。每道工序分别声明可清除的颜色集合；
  只有 "同一内衬、完成时间不早于该接触" 且颜色被某道工序覆盖的旧接触才消除。
* 交班点 ``at`` 处的剩余颜色与来源由时间轴重放得出；更晚的事件不会影响它。
* 安排只占 "交班点 + 舱格"；同一交班点上不同安排不得抢同一格，不同交班点可
  复用。预审多份安排不占格，正式接受一份时才用唯一约束保证互斥。
* 接受时冻结客户版本 (禁配集合) 与原清洗凭证快照；复查永远按原版本、原凭证，
  而不是当前状态或当前规则。
* 清洗单按 *舱格稳定身份 + 内衬* 认定；只在收据上写同一个格号、却落在另一挂车
  的另一只舱格时，不构成对目标舱格的证据。

容量：车场 200 个舱格；订单 500 张；安排 500 份。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterable, Iterator, Sequence

from .warehouse import OUT, STATUS_CONFIRMED, Warehouse

MAX_CELLS = 200
MAX_ORDERS = 500
MAX_ARRANGEMENTS = 500

STATUS_DRAFT = "DRAFT"
STATUS_ACCEPTED = "ACCEPTED"

EVENT_LOAD = "LOAD"
EVENT_EMPTY = "EMPTY"


class CargoError(ValueError):
    """货舱/订单输入或状态错误。"""


class CellBusy(CargoError):
    """同一交班点上该舱格已被另一份已接受安排占用。"""


class OrderBusy(CargoError):
    """同一交班点上该订单已被另一份已接受安排履行。"""


class ArrangementRejected(CargoError):
    """安排无解，或接受时已不再满足资格。"""


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise CargoError(f"{name} must be an integer")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CargoError(f"{name} must be an integer") from exc


def _norm_colors(colors: Iterable[object] | None) -> list[str]:
    out: list[str] = []
    for raw in colors or ():
        s = str(raw).strip()
        if s and s not in out:
            out.append(s)
    return sorted(out)


def _bare(eid: str) -> str:
    return eid.split(":", 1)[1]


YARD_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cell (
    cell_id    TEXT PRIMARY KEY,
    trailer    TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cell_label (
    cell_id TEXT NOT NULL REFERENCES cell(cell_id),
    ts      INTEGER NOT NULL,
    label   TEXT NOT NULL,
    PRIMARY KEY (cell_id, ts)
);
CREATE INDEX IF NOT EXISTS cell_label_lookup ON cell_label(cell_id, ts);

CREATE TABLE IF NOT EXISTS liner (
    liner_id TEXT PRIMARY KEY,
    cell_id  TEXT NOT NULL REFERENCES cell(cell_id),
    from_ts  INTEGER NOT NULL,
    ordinal  INTEGER NOT NULL,
    UNIQUE (cell_id, ordinal),
    UNIQUE (cell_id, from_ts)
);

CREATE TABLE IF NOT EXISTS contact (
    contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id    TEXT NOT NULL REFERENCES cell(cell_id),
    liner_id   TEXT NOT NULL REFERENCES liner(liner_id),
    color      TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    trip_id    TEXT,
    source     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contact_cell ON contact(cell_id, liner_id, ts);

CREATE TABLE IF NOT EXISTS cleaning (
    clean_id   TEXT PRIMARY KEY,
    cell_id    TEXT NOT NULL REFERENCES cell(cell_id),
    liner_id   TEXT NOT NULL REFERENCES liner(liner_id),
    trailer    TEXT NOT NULL,
    label_seen TEXT,
    done_ts    INTEGER NOT NULL,
    trip_id    TEXT,
    steps      TEXT NOT NULL,
    covered    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cleaning_cell ON cleaning(cell_id, liner_id, done_ts);

CREATE TABLE IF NOT EXISTS trip_record (
    trip_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


class Yard:
    """车场侧货舱事实仓。``warehouse`` 用于核对 "已确认运输"，可为 None。"""

    def __init__(self, path: str = ":memory:", warehouse: Warehouse | None = None) -> None:
        self.path = path
        self.warehouse = warehouse
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.executescript(YARD_SCHEMA)

    # ---------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Yard":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---------------------------------------------------------------- cells
    def add_cell(
        self, trailer: str, label: str, *, ts: int, cell_id: str | None = None
    ) -> str:
        """登记一只舱格。``cell_id`` 是稳定身份；``ts`` 起显示 ``label``。"""
        trailer = str(trailer).strip()
        label = str(label).strip()
        ts = _require_int(ts, "ts")
        if not trailer:
            raise CargoError("trailer must be non-empty")
        if not label:
            raise CargoError("label must be non-empty")
        with self.transaction() as c:
            n = int(c.execute("SELECT COUNT(*) AS n FROM cell").fetchone()["n"])
            if n >= MAX_CELLS:
                raise CargoError(f"cell limit {MAX_CELLS} exceeded")
            if cell_id is None:
                cell_id = f"CELL{n + 1:04d}"
            cell_id = str(cell_id).strip()
            if not cell_id:
                raise CargoError("cell_id must be non-empty")
            if c.execute("SELECT 1 FROM cell WHERE cell_id=?", (cell_id,)).fetchone():
                raise CargoError(f"cell {cell_id} already exists")
            clash = self._cells_showing(c, trailer, label, ts)
            if clash:
                raise CargoError(
                    f"trailer {trailer} already shows label {label!r} at ts {ts} "
                    f"on cell {clash[0]}"
                )
            c.execute(
                "INSERT INTO cell(cell_id, trailer, created_ts) VALUES (?,?,?)",
                (cell_id, trailer, ts),
            )
            c.execute(
                "INSERT INTO cell_label(cell_id, ts, label) VALUES (?,?,?)",
                (cell_id, ts, label),
            )
            c.execute(
                "INSERT INTO liner(liner_id, cell_id, from_ts, ordinal) VALUES (?,?,?,?)",
                (f"{cell_id}/L0", cell_id, ts, 0),
            )
        return cell_id

    def rename(self, cell_id: str, label: str, *, ts: int) -> None:
        """改号：只改显示，接触史与内衬都不变。"""
        label = str(label).strip()
        ts = _require_int(ts, "ts")
        if not label:
            raise CargoError("label must be non-empty")
        with self.transaction() as c:
            cell = self._cell_row(c, cell_id)
            if ts < cell["created_ts"]:
                raise CargoError("rename ts precedes cell creation")
            clash = self._cells_showing(c, cell["trailer"], label, ts, exclude=cell_id)
            if clash:
                raise CargoError(
                    f"trailer {cell['trailer']} already shows label {label!r} at ts {ts} "
                    f"on cell {clash[0]}"
                )
            if c.execute(
                "SELECT 1 FROM cell_label WHERE cell_id=? AND ts=?", (cell_id, ts)
            ).fetchone():
                raise CargoError(f"cell {cell_id} already has a label event at ts {ts}")
            c.execute(
                "INSERT INTO cell_label(cell_id, ts, label) VALUES (?,?,?)",
                (cell_id, ts, label),
            )

    def reline(self, cell_id: str, *, ts: int) -> str:
        """重新内衬：创建新内衬，接触史从空开始；旧内衬历史保留。"""
        ts = _require_int(ts, "ts")
        with self.transaction() as c:
            cell = self._cell_row(c, cell_id)
            if ts < cell["created_ts"]:
                raise CargoError("reline ts precedes cell creation")
            last = c.execute(
                "SELECT ordinal, from_ts FROM liner WHERE cell_id=? "
                "ORDER BY ordinal DESC LIMIT 1",
                (cell_id,),
            ).fetchone()
            if ts <= last["from_ts"]:
                raise CargoError(
                    f"reline ts {ts} must be after current liner start {last['from_ts']}"
                )
            ordinal = int(last["ordinal"]) + 1
            liner_id = f"{cell_id}/L{ordinal}"
            c.execute(
                "INSERT INTO liner(liner_id, cell_id, from_ts, ordinal) VALUES (?,?,?,?)",
                (liner_id, cell_id, ts, ordinal),
            )
        return liner_id

    # ---------------------------------------------------------------- lookups
    def _cell_row(self, c: sqlite3.Connection, cell_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM cell WHERE cell_id=?", (cell_id,)).fetchone()
        if row is None:
            raise CargoError(f"unknown cell {cell_id}")
        return row

    def cell(self, cell_id: str) -> dict:
        return dict(self._cell_row(self.conn, cell_id))

    def cells(self) -> list[dict]:
        return [
            dict(r) for r in self.conn.execute("SELECT * FROM cell ORDER BY cell_id")
        ]

    def _label_at(
        self, c: sqlite3.Connection, cell_id: str, at: int
    ) -> str | None:
        row = c.execute(
            "SELECT label FROM cell_label WHERE cell_id=? AND ts<=? "
            "ORDER BY ts DESC LIMIT 1",
            (cell_id, at),
        ).fetchone()
        return None if row is None else row["label"]

    def label_at(self, cell_id: str, at: int) -> str:
        at = _require_int(at, "at")
        self._cell_row(self.conn, cell_id)
        label = self._label_at(self.conn, cell_id, at)
        if label is None:
            raise CargoError(f"cell {cell_id} does not exist at ts {at}")
        return label

    def _cells_showing(
        self,
        c: sqlite3.Connection,
        trailer: str,
        label: str,
        at: int,
        *,
        exclude: str | None = None,
    ) -> list[str]:
        out: list[str] = []
        for r in c.execute("SELECT cell_id FROM cell WHERE trailer=?", (trailer,)):
            cid = r["cell_id"]
            if cid == exclude:
                continue
            if self._label_at(c, cid, at) == label:
                out.append(cid)
        return sorted(out)

    def cell_by_label(self, trailer: str, label: str, at: int) -> str:
        """在 ``at`` 时刻把 (挂车, 显示格号) 解析为唯一的稳定舱格身份。"""
        at = _require_int(at, "at")
        hits = self._cells_showing(self.conn, trailer, label, at)
        if not hits:
            raise CargoError(
                f"no cell on trailer {trailer} shows label {label!r} at ts {at}"
            )
        if len(hits) > 1:
            raise CargoError(
                f"label {label!r} on trailer {trailer} at ts {at} is ambiguous: {hits}"
            )
        return hits[0]

    def _liner_at(self, cell_id: str, at: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM liner WHERE cell_id=? AND from_ts<=? "
            "ORDER BY ordinal DESC LIMIT 1",
            (cell_id, at),
        ).fetchone()

    def liner_at(self, cell_id: str, at: int) -> str:
        at = _require_int(at, "at")
        self._cell_row(self.conn, cell_id)
        row = self._liner_at(cell_id, at)
        if row is None:
            raise CargoError(f"cell {cell_id} has no liner at ts {at}")
        return row["liner_id"]

    # ---------------------------------------------------------------- contacts
    def _insert_contact(
        self,
        c: sqlite3.Connection,
        cell_id: str,
        liner_id: str,
        color: str,
        ts: int,
        trip_id: str | None,
        source: str,
    ) -> None:
        c.execute(
            "INSERT INTO contact(cell_id, liner_id, color, ts, trip_id, source) "
            "VALUES (?,?,?,?,?,?)",
            (cell_id, liner_id, color, ts, trip_id, source),
        )

    def load_from_trip(
        self,
        trip_id: str,
        color: str | None,
        *,
        ts: int | None = None,
        cell_id: str | None = None,
    ) -> dict:
        """按 *已确认* 运输的票面组合与本次用到的舱格，记录放入的纸粒颜色。

        * 运输必须在仓库中为 ``CONFIRMED``。
        * 从运输的 OUT 票取组合产品上的挂车；``cell_id`` 缺省时要求该挂车只有
          一只已登记舱格，否则必须显式指定 (一只挂车可有多个舱格 A/B)。
        * ``color=None`` 表示空跑：不新增任何颜色。
        * ``ts`` 缺省取该运输 OUT 票的时间。
        """
        if self.warehouse is None:
            raise CargoError("yard has no warehouse to resolve confirmed trips")
        ev = self.warehouse.evaluation(trip_id)
        trip = self.warehouse.trip(trip_id)
        if trip["status"] != STATUS_CONFIRMED:
            raise CargoError(f"trip {trip_id} is not confirmed; cannot have placed cargo")
        out_product = ev.derived_out.product
        if out_product.trailer_id is None:
            raise CargoError(f"trip {trip_id} has no trailer at OUT; no hold to fill")
        trailer = _bare(out_product.trailer_id)
        out_ticket = self.warehouse.ticket(
            self.warehouse.trip(trip_id)["tickets"][OUT]["ticket_id"]
        )
        at = out_ticket.ts if ts is None else _require_int(ts, "ts")
        color_norm = None if color is None else str(color).strip()
        if color is None or color_norm == "":
            color_norm = None
        with self.transaction() as c:
            # 幂等：同一 trip 重复登记必须一致。
            if cell_id is not None:
                self._cell_row(c, cell_id)
                target = cell_id
                if c.execute(
                    "SELECT 1 FROM cell WHERE cell_id=? AND trailer=?",
                    (target, trailer),
                ).fetchone() is None:
                    raise CargoError(
                        f"cell {target} is not on trailer {trailer} of trip {trip_id}"
                    )
            else:
                cells = [
                    r["cell_id"]
                    for r in c.execute(
                        "SELECT cell_id FROM cell WHERE trailer=? ORDER BY cell_id",
                        (trailer,),
                    )
                ]
                if len(cells) != 1:
                    raise CargoError(
                        f"trailer {trailer} has {len(cells)} registered cells; "
                        f"pass cell_id explicitly"
                    )
                target = cells[0]
            payload = json.dumps(
                {
                    "trip_id": trip_id, "trailer": trailer, "cell_id": target,
                    "color": color_norm, "ts": at,
                },
                sort_keys=True,
            )
            prior = c.execute(
                "SELECT payload FROM trip_record WHERE trip_id=?", (trip_id,)
            ).fetchone()
            if prior is not None:
                if prior["payload"] != payload:
                    raise CargoError(
                        f"trip {trip_id} already recorded with different cargo"
                    )
                return json.loads(payload)
            liner = self._liner_at(target, at)
            if liner is None:
                raise CargoError(f"cell {target} has no liner at ts {at}")
            if color_norm is not None:
                self._insert_contact(
                    c, target, liner["liner_id"], color_norm, at, trip_id, EVENT_LOAD
                )
            c.execute(
                "INSERT INTO trip_record(trip_id, payload) VALUES (?,?)",
                (trip_id, payload),
            )
        return json.loads(payload)

    def load_cargo(
        self, cell_id: str, color: str | None, *, ts: int, trip_id: str | None = None
    ) -> None:
        """手工登记一次放入 (无仓库或离线补录时使用)。``color=None`` 为空跑。"""
        ts = _require_int(ts, "ts")
        color_norm = None if color is None else str(color).strip()
        if color_norm is not None and color_norm == "":
            color_norm = None
        with self.transaction() as c:
            cell = self._cell_row(c, cell_id)
            if ts < cell["created_ts"]:
                raise CargoError("contact ts precedes cell creation")
            if color_norm is None:
                return  # 空跑：什么都不新增
            liner = self._liner_at(cell_id, ts)
            if liner is None:
                raise CargoError(f"cell {cell_id} has no liner at ts {ts}")
            self._insert_contact(
                c, cell_id, liner["liner_id"], color_norm, ts, trip_id, EVENT_LOAD
            )

    # ---------------------------------------------------------------- cleaning
    def clean(
        self,
        cell_id: str,
        *,
        done_ts: int,
        steps: Sequence[dict],
        trailer: str | None = None,
        label_seen: str | None = None,
        trip_id: str | None = None,
        cell_hint: str | None = None,
    ) -> dict:
        """登记清洗单。

        ``steps`` 为 ``[{"step": 名称, "clears": [颜色,...]}, ...]``；每道工序
        分别声明可清除的颜色集合。清洗证据只作用于 *完成时间当时的内衬*，且只
        消除完成时间不早于自身 ``done_ts`` 的接触。

        ``trailer`` / ``label_seen`` 是收据上写的内容；若同时给出 ``cell_hint``
        (另一只舱格的稳定身份)，则收据即使写同一个格号，也不会把证据挂到目标
        舱格——这正是 "同车牌接另一挂车、收据同样写 A 格" 的不可行来源。
        """
        done_ts = _require_int(done_ts, "done_ts")
        if not steps:
            raise CargoError("cleaning slip needs at least one step")
        norm_steps: list[dict] = []
        covered: set[str] = set()
        for i, raw in enumerate(steps):
            name = str(raw.get("step", f"step{i + 1}")).strip()
            if not name:
                raise CargoError("cleaning step name must be non-empty")
            clears = _norm_colors(raw.get("clears"))
            covered.update(clears)
            norm_steps.append({"step": name, "clears": clears})
        with self.transaction() as c:
            cell = self._cell_row(c, cell_id)
            if done_ts < cell["created_ts"]:
                raise CargoError("cleaning done_ts precedes cell creation")
            # 识别人为指定的 "另一只舱格"：证据不落到目标 cell_id。
            if cell_hint is not None and str(cell_hint).strip() != cell_id:
                raise CargoError(
                    f"cleaning receipt for cell {cell_id} actually served cell "
                    f"{cell_hint}; evidence does not cover {cell_id}"
                )
            if trailer is not None and str(trailer).strip() != cell["trailer"]:
                raise CargoError(
                    f"cleaning receipt trailer {trailer!r} does not match cell "
                    f"{cell_id} on trailer {cell['trailer']!r}"
                )
            liner = self._liner_at(cell_id, done_ts)
            if liner is None:
                raise CargoError(f"cell {cell_id} has no liner at ts {done_ts}")
            if c.execute(
                "SELECT 1 FROM cleaning WHERE cell_id=? AND liner_id=? AND done_ts=?",
                (cell_id, liner["liner_id"], done_ts),
            ).fetchone():
                raise CargoError(
                    f"cleaning slip already registered for {cell_id} at ts {done_ts}"
                )
            clean_id = f"CLN-{cell_id}-{done_ts}"
            c.execute(
                "INSERT INTO cleaning(clean_id, cell_id, liner_id, trailer, label_seen, "
                "done_ts, trip_id, steps, covered) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    clean_id, cell_id, liner["liner_id"], cell["trailer"], label_seen,
                    done_ts, trip_id,
                    json.dumps(norm_steps, sort_keys=True, ensure_ascii=False),
                    json.dumps(sorted(covered)),
                ),
            )
        return self.cleaning(clean_id)

    def cleaning(self, clean_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM cleaning WHERE clean_id=?", (clean_id,)
        ).fetchone()
        if row is None:
            raise CargoError(f"unknown cleaning slip {clean_id}")
        return self._cleaning_dict(row)

    def _cleaning_dict(self, row: sqlite3.Row) -> dict:
        return {
            "clean_id": row["clean_id"],
            "cell_id": row["cell_id"],
            "liner_id": row["liner_id"],
            "trailer": row["trailer"],
            "label_seen": row["label_seen"],
            "done_ts": int(row["done_ts"]),
            "trip_id": row["trip_id"],
            "steps": json.loads(row["steps"]),
            "covered": json.loads(row["covered"]),
        }

    def cleanings(self, cell_id: str | None = None) -> list[dict]:
        if cell_id is None:
            rows = self.conn.execute(
                "SELECT * FROM cleaning ORDER BY done_ts, clean_id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM cleaning WHERE cell_id=? ORDER BY done_ts, clean_id",
                (cell_id,),
            ).fetchall()
        return [self._cleaning_dict(r) for r in rows]

    # ---------------------------------------------------------------- replay
    def residual_colors(self, cell_id: str, at: int) -> list[dict]:
        """``at`` 时刻该舱格的剩余颜色及来源。

        只重放 *当前内衬* (``at`` 时刻生效的内衬) 上的接触与清洗：重新内衬会
        开始新的接触历史，旧内衬的接触不再计入剩余颜色 (但仍留在接触史与清洗
        记录里可追溯)。每条剩余颜色附带 ``color``、``since`` (最早未消除接触
        时间)、``contact_ids``、``sources`` 及 ``liner_id``。清洗只消除 "同内衬
        + 完成时间不早于该接触" 且颜色被工序覆盖的接触。
        """
        at = _require_int(at, "at")
        self._cell_row(self.conn, cell_id)
        liner = self._liner_at(cell_id, at)
        if liner is None:
            return []
        liner_id = liner["liner_id"]
        contacts = self.conn.execute(
            "SELECT * FROM contact WHERE cell_id=? AND liner_id=? AND ts<=? "
            "ORDER BY ts, contact_id",
            (cell_id, liner_id, at),
        ).fetchall()
        cleanings = self.conn.execute(
            "SELECT * FROM cleaning WHERE cell_id=? AND liner_id=? AND done_ts<=? "
            "ORDER BY done_ts, clean_id",
            (cell_id, liner_id, at),
        ).fetchall()
        residual: dict[str, dict] = {}
        for ct in contacts:
            entry = residual.setdefault(
                ct["color"],
                {
                    "color": ct["color"],
                    "liner_id": ct["liner_id"],
                    "contact_ids": [],
                    "sources": [],
                    "since": int(ct["ts"]),
                    "first_contact_id": int(ct["contact_id"]),
                },
            )
            entry["contact_ids"].append(int(ct["contact_id"]))
            entry["sources"].append(
                {
                    "contact_id": int(ct["contact_id"]),
                    "ts": int(ct["ts"]),
                    "liner_id": ct["liner_id"],
                    "trip_id": ct["trip_id"],
                    "source": ct["source"],
                }
            )
        # 逐张清洗单消除它覆盖、且同内衬、且 done_ts >= 接触 ts 的接触。
        for cl in cleanings:
            covered = set(json.loads(cl["covered"]))
            for color, entry in residual.items():
                if color not in covered:
                    continue
                kept_sources = []
                kept_ids = []
                for src in entry["sources"]:
                    if src["liner_id"] == cl["liner_id"] and src["ts"] <= cl["done_ts"]:
                        continue  # 被这份证据消除
                    kept_sources.append(src)
                    kept_ids.append(src["contact_id"])
                entry["sources"] = kept_sources
                entry["contact_ids"] = kept_ids
                if kept_sources:
                    entry["since"] = min(s["ts"] for s in kept_sources)
        return [residual[k] for k in sorted(residual) if residual[k]["contact_ids"]]

    def residual_color_set(self, cell_id: str, at: int) -> set[str]:
        return {e["color"] for e in self.residual_colors(cell_id, at)}

    def handover(self, at: int, *, cells: Sequence[str] | None = None) -> dict:
        """交班点全景：每个舱格在该时刻的内衬、显示格号与剩余颜色及来源。"""
        at = _require_int(at, "at")
        ids = (
            [str(x) for x in cells]
            if cells is not None
            else [r["cell_id"] for r in self.conn.execute("SELECT cell_id FROM cell ORDER BY cell_id")]
        )
        out: dict[str, dict] = {}
        for cid in ids:
            cell = self._cell_row(self.conn, cid)
            if at < cell["created_ts"]:
                out[cid] = {
                    "cell_id": cid, "trailer": cell["trailer"],
                    "label": None, "liner_id": None, "residual": [],
                    "exists": False,
                }
                continue
            out[cid] = {
                "cell_id": cid,
                "trailer": cell["trailer"],
                "label": self._label_at(self.conn, cid, at),
                "liner_id": self._liner_at(cid, at)["liner_id"],
                "residual": self.residual_colors(cid, at),
                "exists": True,
            }
        return {"at": at, "cells": out}

    # ---------------------------------------------------------------- integrity
    def reconcile(self) -> list[str]:
        errs: list[str] = []
        for r in self.conn.execute(
            "SELECT c.cell_id FROM cell c WHERE NOT EXISTS ("
            " SELECT 1 FROM cell_label l WHERE l.cell_id=c.cell_id)"
        ):
            errs.append(f"cell {r['cell_id']} has no label history")
        for r in self.conn.execute(
            "SELECT c.cell_id FROM cell c WHERE NOT EXISTS ("
            " SELECT 1 FROM liner n WHERE n.cell_id=c.cell_id AND n.ordinal=0)"
        ):
            errs.append(f"cell {r['cell_id']} has no initial liner")
        for r in self.conn.execute(
            "SELECT ct.contact_id FROM contact ct "
            "LEFT JOIN liner n ON n.liner_id=ct.liner_id "
            "WHERE n.liner_id IS NULL OR n.cell_id<>ct.cell_id"
        ):
            errs.append(f"contact {r['contact_id']} is not on its cell's liner")
        for r in self.conn.execute(
            "SELECT cl.clean_id FROM cleaning cl "
            "LEFT JOIN liner n ON n.liner_id=cl.liner_id "
            "WHERE n.liner_id IS NULL OR n.cell_id<>cl.cell_id"
        ):
            errs.append(f"cleaning {r['clean_id']} is not on its cell's liner")
        return errs


ORDER_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ban_rule (
    customer_version TEXT NOT NULL,
    load_color       TEXT NOT NULL,
    banned_color     TEXT NOT NULL,
    PRIMARY KEY (customer_version, load_color, banned_color)
);

CREATE TABLE IF NOT EXISTS customer_order (
    order_id         TEXT PRIMARY KEY,
    intend_color     TEXT NOT NULL,
    customer_version TEXT NOT NULL,
    point            INTEGER NOT NULL,
    allowed_labels   TEXT NOT NULL DEFAULT '[]',
    created_order    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS arrangement (
    arr_id         TEXT PRIMARY KEY,
    point          INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('DRAFT','ACCEPTED')),
    orders         TEXT NOT NULL,
    mapping        TEXT NOT NULL,
    frozen_rules   TEXT NOT NULL DEFAULT '{}',
    frozen_evidence TEXT NOT NULL DEFAULT '{}',
    impedance      TEXT NOT NULL DEFAULT '{}',
    note           TEXT NOT NULL DEFAULT '',
    created_order  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS occupancy (
    arr_id   TEXT NOT NULL REFERENCES arrangement(arr_id),
    point    INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    cell_id  TEXT NOT NULL,
    PRIMARY KEY (arr_id, order_id),
    UNIQUE (point, order_id),
    UNIQUE (point, cell_id)
);
"""


def _max_matching(
    order_ids: Sequence[str], candidates: dict[str, list[str]]
) -> tuple[dict[str, str], dict[str, str]]:
    """二分图最大匹配 (Kuhn)。返回 (order->cell, cell->order)。"""
    match_cell: dict[str, str] = {}
    match_order: dict[str, str] = {}

    def try_kuhn(order: str, visited: set[str]) -> bool:
        for cell in candidates.get(order, []):
            if cell in visited:
                continue
            visited.add(cell)
            occupant = match_cell.get(cell)
            if occupant is None or try_kuhn(occupant, visited):
                match_cell[cell] = order
                match_order[order] = cell
                return True
        return False

    for order in order_ids:
        try_kuhn(order, set())
    return match_order, match_cell


def _hall_violator(
    order_ids: Sequence[str],
    candidates: dict[str, list[str]],
    match_order: dict[str, str],
    match_cell: dict[str, str],
) -> tuple[list[str], list[str]]:
    """最大匹配失败时，求一个 Hall 阻碍集 S 及其邻域 N(S)，|N(S)| < |S|。"""
    unmatched = [o for o in order_ids if o not in match_order]
    reach_orders = set(unmatched)
    reach_cells: set[str] = set()
    stack = list(unmatched)
    while stack:
        order = stack.pop()
        for cell in candidates.get(order, []):
            if cell in reach_cells:
                continue
            reach_cells.add(cell)
            occupant = match_cell.get(cell)
            if occupant is not None and occupant not in reach_orders:
                reach_orders.add(occupant)
                stack.append(occupant)
    return sorted(reach_orders), sorted(reach_cells)


class OrderBook:
    """订货方侧：客户版本禁配集合、订单批次、安排预审/接受与冻结复查。"""

    def __init__(self, path: str = ":memory:", yard: Yard | None = None) -> None:
        self.path = path
        self.yard = yard
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.executescript(ORDER_SCHEMA)

    # ---------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "OrderBook":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---------------------------------------------------------------- rules
    def set_ban(
        self, customer_version: str, load_color: str, banned_colors: Iterable[object]
    ) -> None:
        """按 (拟装颜色, 客户版本) 登记禁配集合：这些旧接触会令该格不合格。"""
        version = str(customer_version).strip()
        load = str(load_color).strip()
        if not version or not load:
            raise CargoError("customer_version and load_color must be non-empty")
        banned = _norm_colors(banned_colors)
        with self.transaction() as c:
            c.execute(
                "DELETE FROM ban_rule WHERE customer_version=? AND load_color=?",
                (version, load),
            )
            for color in banned:
                c.execute(
                    "INSERT INTO ban_rule(customer_version, load_color, banned_color) "
                    "VALUES (?,?,?)",
                    (version, load, color),
                )

    def ban_set(self, customer_version: str, load_color: str) -> set[str]:
        return {
            r["banned_color"]
            for r in self.conn.execute(
                "SELECT banned_color FROM ban_rule WHERE customer_version=? AND load_color=?",
                (str(customer_version).strip(), str(load_color).strip()),
            )
        }

    # ---------------------------------------------------------------- orders
    def add_order(
        self,
        order_id: str,
        intend_color: str,
        customer_version: str,
        *,
        point: int,
        allowed_labels: Iterable[object] | None = None,
    ) -> dict:
        oid = str(order_id).strip()
        color = str(intend_color).strip()
        version = str(customer_version).strip()
        point = _require_int(point, "point")
        if not oid or not color or not version:
            raise CargoError("order_id, intend_color, customer_version must be non-empty")
        allowed = _norm_colors(allowed_labels)
        with self.transaction() as c:
            row = c.execute(
                "SELECT * FROM customer_order WHERE order_id=?", (oid,)
            ).fetchone()
            if row is not None:
                expected = (color, version, point, allowed)
                actual = (
                    row["intend_color"], row["customer_version"], int(row["point"]),
                    json.loads(row["allowed_labels"]),
                )
                if expected != actual:
                    raise CargoError(f"order {oid} exists with different data")
                return self.order(oid)
            n = int(
                c.execute("SELECT COALESCE(MAX(created_order),0)+1 AS n FROM customer_order")
                .fetchone()["n"]
            )
            if n > MAX_ORDERS:
                raise CargoError(f"order limit {MAX_ORDERS} exceeded")
            c.execute(
                "INSERT INTO customer_order(order_id, intend_color, customer_version, "
                "point, allowed_labels, created_order) VALUES (?,?,?,?,?,?)",
                (oid, color, version, point, json.dumps(allowed), n),
            )
        return self.order(oid)

    def order(self, order_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM customer_order WHERE order_id=?", (str(order_id),)
        ).fetchone()
        if row is None:
            raise CargoError(f"unknown order {order_id}")
        return {
            "order_id": row["order_id"],
            "intend_color": row["intend_color"],
            "customer_version": row["customer_version"],
            "point": int(row["point"]),
            "allowed_labels": json.loads(row["allowed_labels"]),
        }

    def orders(self, *, point: int | None = None) -> list[dict]:
        if point is None:
            rows = self.conn.execute(
                "SELECT order_id FROM customer_order ORDER BY created_order"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT order_id FROM customer_order WHERE point=? ORDER BY created_order",
                (_require_int(point, "point"),),
            ).fetchall()
        return [self.order(r["order_id"]) for r in rows]

    # ---------------------------------------------------------------- eligibility
    def _check_order_against(
        self,
        order: dict,
        cell_id: str,
        at: int,
        *,
        yard: Yard,
        rules: dict[tuple[str, str], set[str]],
    ) -> dict:
        """返回 ``{"eligible": bool, "reasons": [...]}`` 及完整证据。"""
        reasons: list[str] = []
        try:
            info = yard.handover(at, cells=[cell_id])["cells"][cell_id]
        except CargoError as exc:
            return {"eligible": False, "reasons": [str(exc)], "residual": []}
        if not info["exists"]:
            return {"eligible": False, "reasons": ["cell does not exist at handover"],
                    "residual": [], "label": None, "liner_id": None}
        label = info["label"]
        allowed = order["allowed_labels"]
        if allowed and label not in allowed:
            reasons.append(
                f"label {label!r} not in allowed {allowed}"
            )
        banned = rules.get((order["customer_version"], order["intend_color"]), set())
        residual = info["residual"]
        clash = sorted(
            {e["color"] for e in residual if e["color"] in banned}
        )
        if clash:
            reasons.append(f"residual colors {clash} are banned for {order['intend_color']}")
        return {
            "eligible": not reasons,
            "reasons": reasons,
            "label": label,
            "liner_id": info["liner_id"],
            "residual": residual,
            "banned": sorted(banned),
            "clash": clash,
        }

    def _rules_snapshot(self, *, yard: Yard) -> dict:
        # 冻结整张规则表，确保复查按 "原版本" 而非之后追加的版本。
        snap: dict[str, dict[str, list[str]]] = {}
        for r in self.conn.execute(
            "SELECT customer_version, load_color, banned_color FROM ban_rule "
            "ORDER BY customer_version, load_color, banned_color"
        ):
            snap.setdefault(r["customer_version"], {}).setdefault(
                r["load_color"], []
            ).append(r["banned_color"])
        return snap

    @staticmethod
    def _rules_from_snapshot(frozen: dict) -> dict[tuple[str, str], set[str]]:
        out: dict[tuple[str, str], set[str]] = {}
        for version, by_color in frozen.items():
            for load, banned in by_color.items():
                out[(version, load)] = set(banned)
        return out

    def candidates(
        self, point: int, *, yard: Yard | None = None
    ) -> dict[str, dict]:
        """逐单独立候选：每单在 ``point`` 有哪些合格舱格及理由。"""
        y = yard or self.yard
        if y is None:
            raise CargoError("no yard to evaluate orders against")
        point = _require_int(point, "point")
        rules: dict[tuple[str, str], set[str]] = {}
        for r in self.conn.execute(
            "SELECT customer_version, load_color, banned_color FROM ban_rule"
        ):
            rules.setdefault((r["customer_version"], r["load_color"]), set()).add(
                r["banned_color"]
            )
        cell_ids = [r["cell_id"] for r in y.conn.execute(
            "SELECT cell_id FROM cell ORDER BY cell_id"
        )]
        out: dict[str, dict] = {}
        for order in self.orders(point=point):
            per_cell: dict[str, dict] = {}
            eligible: list[str] = []
            for cid in cell_ids:
                res = self._check_order_against(
                    order, cid, point, yard=y, rules=rules
                )
                per_cell[cid] = res
                if res["eligible"]:
                    eligible.append(cid)
            out[order["order_id"]] = {
                "order": order,
                "cells": per_cell,
                "eligible": eligible,
            }
        return out

    # ---------------------------------------------------------------- matching
    def solve_point(self, point: int, *, yard: Yard | None = None) -> dict:
        """对 ``point`` 整批订单做二分图匹配。

        返回 ``{"feasible", "matching", "unmatched", "impediments", "candidates"}``。
        无解时 ``impediments`` 含 Hall 阻碍集，并逐单列出其无候选/被抢的原因。
        """
        point = _require_int(point, "point")
        cand = self.candidates(point, yard=yard)
        order_ids = list(cand.keys())
        edges = {oid: cand[oid]["eligible"] for oid in order_ids}
        match_order, match_cell = _max_matching(order_ids, edges)
        unmatched = [oid for oid in order_ids if oid not in match_order]
        impediments: dict = {}
        feasible = not unmatched
        if not feasible:
            s_orders, s_cells = _hall_violator(order_ids, edges, match_order, match_cell)
            detail = []
            for oid in s_orders:
                order = cand[oid]["order"]
                per_cell = cand[oid]["cells"]
                if not per_cell:
                    detail.append({
                        "order_id": oid, "cause": "NO_CELLS",
                        "explain": "no cells registered at all",
                    })
                    continue
                if not edges[oid]:
                    blocked = {
                        cid: r["reasons"] for cid, r in per_cell.items() if r["reasons"]
                    }
                    detail.append({
                        "order_id": oid, "cause": "NO_ELIGIBLE_CELL",
                        "intend_color": order["intend_color"],
                        "customer_version": order["customer_version"],
                        "blocked": blocked,
                    })
                else:
                    detail.append({
                        "order_id": oid, "cause": "CONTENTION",
                        "cells": edges[oid],
                        "explain": "eligible cells are also needed by other orders "
                                   "in the same handover point",
                    })
            impediments = {
                "hall_orders": s_orders,
                "hall_cells": s_cells,
                "detail": detail,
                "note": (
                    f"|orders|={len(s_orders)} > |reachable cells|={len(s_cells)}; "
                    "each order needs a distinct cell at the same handover point"
                ),
            }
        return {
            "point": point,
            "feasible": feasible,
            "matching": dict(sorted(match_order.items())),
            "unmatched": unmatched,
            "impediments": impediments,
            "candidates": {
                oid: {
                    "eligible": cand[oid]["eligible"],
                    "intend_color": cand[oid]["order"]["intend_color"],
                    "customer_version": cand[oid]["order"]["customer_version"],
                }
                for oid in order_ids
            },
        }

    # ---------------------------------------------------------------- preview
    def preview(
        self,
        order_ids: Sequence[str],
        *,
        point: int | None = None,
        yard: Yard | None = None,
        note: str = "",
    ) -> dict:
        """预审一份安排：只计算，不占格。多份预审可并存且互不影响。"""
        y = yard or self.yard
        if y is None:
            raise CargoError("no yard to evaluate arrangement against")
        ids = [str(o).strip() for o in order_ids]
        if not ids:
            raise CargoError("arrangement needs at least one order")
        if len(set(ids)) != len(ids):
            raise CargoError("duplicate order in arrangement")
        ords = [self.order(o) for o in ids]
        if point is None:
            pts = {o["point"] for o in ords}
            if len(pts) != 1:
                raise CargoError("orders span multiple handover points; pass point=")
            point = pts.pop()
        point = _require_int(point, "point")
        for o in ords:
            if o["point"] != point:
                raise CargoError(
                    f"order {o['order_id']} belongs to point {o['point']}, not {point}"
                )
        # 资格按当前规则 *与当前车场状态* 计算；冻结快照在 accept 时生成。
        rules = self._rules_from_snapshot(self._rules_snapshot(yard=y))
        per_order: dict[str, dict] = {}
        edges: dict[str, list[str]] = {}
        cell_ids = [r["cell_id"] for r in y.conn.execute(
            "SELECT cell_id FROM cell ORDER BY cell_id"
        )]
        for o in ords:
            cells = {}
            for cid in cell_ids:
                cells[cid] = self._check_order_against(
                    o, cid, point, yard=y, rules=rules
                )
            per_order[o["order_id"]] = cells
            edges[o["order_id"]] = [cid for cid in cell_ids if cells[cid]["eligible"]]
        match_order, match_cell = _max_matching(ids, edges)
        unmatched = [o for o in ids if o not in match_order]
        feasible = not unmatched
        impediments: dict = {}
        if not feasible:
            s_orders, s_cells = _hall_violator(ids, edges, match_order, match_cell)
            impediments = {
                "hall_orders": s_orders,
                "hall_cells": s_cells,
                "detail": [
                    {
                        "order_id": o,
                        "eligible": edges[o],
                        "blocked": {
                            cid: per_order[o][cid]["reasons"]
                            for cid in cell_ids
                            if per_order[o][cid]["reasons"]
                        },
                    }
                    for o in s_orders
                ],
                "note": "same handover point: each order needs a distinct cell",
            }
        with self.transaction() as c:
            n = int(c.execute(
                "SELECT COALESCE(MAX(created_order),0)+1 AS n FROM arrangement"
            ).fetchone()["n"])
            if n > MAX_ARRANGEMENTS:
                raise CargoError(f"arrangement limit {MAX_ARRANGEMENTS} exceeded")
            arr_id = f"ARR{n:04d}"
            c.execute(
                "INSERT INTO arrangement(arr_id, point, status, orders, mapping, "
                "frozen_rules, frozen_evidence, impedance, note, created_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    arr_id, point, STATUS_DRAFT, json.dumps(ids),
                    json.dumps(dict(sorted(match_order.items()))),
                    "{}", "{}",
                    json.dumps(impediments, sort_keys=True, default=str),
                    note, n,
                ),
            )
        return self.arrangement(arr_id)

    def arrangement(self, arr_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM arrangement WHERE arr_id=?", (str(arr_id),)
        ).fetchone()
        if row is None:
            raise CargoError(f"unknown arrangement {arr_id}")
        return self._arr_dict(row)

    def _arr_dict(self, row: sqlite3.Row) -> dict:
        return {
            "arr_id": row["arr_id"],
            "point": int(row["point"]),
            "status": row["status"],
            "orders": json.loads(row["orders"]),
            "mapping": json.loads(row["mapping"]),
            "impedance": json.loads(row["impedance"]),
            "note": row["note"],
            "frozen_rules": json.loads(row["frozen_rules"]),
            "frozen_evidence": json.loads(row["frozen_evidence"]),
        }

    def arrangements(self) -> list[dict]:
        return [
            self._arr_dict(r)
            for r in self.conn.execute(
                "SELECT * FROM arrangement ORDER BY created_order"
            )
        ]

    # ---------------------------------------------------------------- accept
    def _freeze_evidence(
        self, yard: Yard, point: int, mapping: dict[str, str]
    ) -> dict:
        evidence: dict[str, dict] = {}
        for oid, cid in mapping.items():
            info = yard.handover(point, cells=[cid])["cells"][cid]
            slips = [
                s for s in yard.cleanings(cid)
                if s["done_ts"] <= point and s["liner_id"] == info["liner_id"]
            ]
            evidence[oid] = {
                "cell_id": cid,
                "label": info["label"],
                "liner_id": info["liner_id"],
                "residual": info["residual"],
                "cleanings": slips,
            }
        return evidence

    def accept(self, arr_id: str, *, yard: Yard | None = None) -> dict:
        """正式接受一份安排：重新校验并冻结证据；同一格/单不得被重复占用。"""
        y = yard or self.yard
        if y is None:
            raise CargoError("no yard to accept arrangement against")
        with self.transaction() as c:
            row = c.execute(
                "SELECT * FROM arrangement WHERE arr_id=?", (str(arr_id),)
            ).fetchone()
            if row is None:
                raise CargoError(f"unknown arrangement {arr_id}")
            if row["status"] == STATUS_ACCEPTED:
                return self._arr_dict(row)
            point = int(row["point"])
            ids = json.loads(row["orders"])
            rules = self._rules_from_snapshot(self._rules_snapshot(yard=y))
            cell_ids = [r["cell_id"] for r in y.conn.execute(
                "SELECT cell_id FROM cell ORDER BY cell_id"
            )]
            # 已被 *其它* 已接受安排在这一点占用的订单 / 舱格。
            busy_orders = {
                r["order_id"]
                for r in c.execute(
                    "SELECT order_id FROM occupancy WHERE point=? AND arr_id<>?",
                    (point, arr_id),
                )
            }
            busy_cells = {
                r["cell_id"]
                for r in c.execute(
                    "SELECT cell_id FROM occupancy WHERE point=? AND arr_id<>?",
                    (point, arr_id),
                )
            }
            for oid in ids:
                if oid in busy_orders:
                    raise OrderBusy(
                        f"order {oid} already fulfilled at point {point}"
                    )
            edges: dict[str, list[str]] = {}
            for oid in ids:
                order = self.order(oid)
                if order["point"] != point:
                    raise ArrangementRejected(
                        f"order {oid} no longer belongs to point {point}"
                    )
                elig = [
                    cid for cid in cell_ids
                    if cid not in busy_cells
                    and self._check_order_against(
                        order, cid, point, yard=y, rules=rules
                    )["eligible"]
                ]
                edges[oid] = elig
            match_order, _ = _max_matching(ids, edges)
            if len(match_order) != len(ids):
                unmatched = [o for o in ids if o not in match_order]
                # 若某未匹配单仅因舱格已被别的安排占用而无候选，报 CellBusy。
                for oid in unmatched:
                    order = self.order(oid)
                    naive = [
                        cid for cid in cell_ids
                        if self._check_order_against(
                            order, cid, point, yard=y, rules=rules
                        )["eligible"]
                    ]
                    if naive and not edges[oid]:
                        raise CellBusy(
                            f"cell(s) {naive} needed by order {oid} are already "
                            f"occupied at point {point}"
                        )
                raise ArrangementRejected(
                    f"arrangement {arr_id} is no longer feasible; unmatched {unmatched}"
                )
            mapping = dict(sorted(match_order.items()))
            evidence = self._freeze_evidence(y, point, mapping)
            try:
                for oid, cid in mapping.items():
                    c.execute(
                        "INSERT INTO occupancy(arr_id, point, order_id, cell_id) "
                        "VALUES (?,?,?,?)",
                        (arr_id, point, oid, cid),
                    )
            except sqlite3.IntegrityError as exc:
                raise CellBusy(
                    f"arrangement {arr_id} cannot be accepted: collision on point "
                    f"{point} ({exc})"
                ) from exc
            c.execute(
                "UPDATE arrangement SET status=?, mapping=?, frozen_rules=?, "
                "frozen_evidence=? WHERE arr_id=?",
                (
                    STATUS_ACCEPTED,
                    json.dumps(mapping),
                    json.dumps(self._rules_snapshot(yard=y), sort_keys=True),
                    json.dumps(evidence, sort_keys=True, ensure_ascii=False),
                    arr_id,
                ),
            )
        return self.arrangement(arr_id)

    # ---------------------------------------------------------------- recheck
    def recheck(self, arr_id: str) -> dict:
        """按 *原客户版本* (冻结规则) 与 *原清洗凭证* (冻结证据) 复查已接受安排。

        不读当前车场/当前规则，因此之后的内衬更换、清洗、或对新版本追加的禁配
        都不会改变这次复查结论。
        """
        arr = self.arrangement(arr_id)
        if arr["status"] != STATUS_ACCEPTED:
            raise CargoError(f"arrangement {arr_id} is not accepted; nothing to recheck")
        frozen_rules = self._rules_from_snapshot(arr["frozen_rules"])
        evidence = arr["frozen_evidence"]
        per_order: dict[str, dict] = {}
        ok = True
        for oid in arr["orders"]:
            order = self.order(oid)
            ev = evidence.get(oid)
            if ev is None:
                ok = False
                per_order[oid] = {"eligible": False, "reasons": ["missing frozen evidence"]}
                continue
            banned = frozen_rules.get(
                (order["customer_version"], order["intend_color"]), set()
            )
            residual_colors = sorted({e["color"] for e in ev["residual"]})
            clash = sorted(set(residual_colors) & banned)
            reasons = []
            if clash:
                reasons.append(f"residual {clash} banned under original version "
                               f"{order['customer_version']}")
            allowed = order["allowed_labels"]
            if allowed and ev["label"] not in allowed:
                reasons.append(f"labels {ev['label']} not in allowed {allowed}")
            if reasons:
                ok = False
            per_order[oid] = {
                "eligible": not reasons,
                "reasons": reasons,
                "cell_id": ev["cell_id"],
                "label": ev["label"],
                "liner_id": ev["liner_id"],
                "customer_version": order["customer_version"],
                "intend_color": order["intend_color"],
                "banned": sorted(banned),
                "residual": residual_colors,
                # 原清洗凭证：复查时列明当时生效的清洗单
                "cleaning_slips": [
                    {
                        "clean_id": s["clean_id"],
                        "done_ts": s["done_ts"],
                        "steps": s["steps"],
                        "covered": s["covered"],
                    }
                    for s in ev["cleanings"]
                ],
            }
        return {
            "arr_id": arr_id,
            "point": arr["point"],
            "accepted_ok": ok,
            "orders": per_order,
            "frozen_rule_versions": sorted(arr["frozen_rules"].keys()),
        }

    def live_status(self, arr_id: str, *, yard: Yard | None = None) -> dict:
        """对照 *当前* 车场状态重新核验已接受安排 (不改变冻结复查结论)。"""
        y = yard or self.yard
        if y is None:
            raise CargoError("no yard to compare against")
        arr = self.arrangement(arr_id)
        if arr["status"] != STATUS_ACCEPTED:
            raise CargoError(f"arrangement {arr_id} is not accepted")
        mapping = arr["mapping"]
        rules = self._rules_from_snapshot(self._rules_snapshot(yard=y))
        per_order: dict[str, dict] = {}
        ok = True
        for oid in arr["orders"]:
            order = self.order(oid)
            cid = mapping.get(oid)
            if cid is None:
                ok = False
                per_order[oid] = {"eligible": False, "reasons": ["missing mapping"]}
                continue
            res = self._check_order_against(order, cid, arr["point"], yard=y, rules=rules)
            if not res["eligible"]:
                ok = False
            per_order[oid] = {
                "eligible": res["eligible"],
                "reasons": res["reasons"],
                "cell_id": cid,
                "residual": sorted({e["color"] for e in res.get("residual", [])}),
            }
        return {"arr_id": arr_id, "still_ok": ok, "orders": per_order}

    # ---------------------------------------------------------------- integrity
    def occupancy(self, arr_id: str) -> list[dict]:
        return [
            dict(r) for r in self.conn.execute(
                "SELECT arr_id, point, order_id, cell_id FROM occupancy "
                "WHERE arr_id=? ORDER BY order_id",
                (str(arr_id),),
            )
        ]

    def reconcile(self) -> list[str]:
        errs: list[str] = []
        for r in self.conn.execute(
            "SELECT arr.arr_id, COUNT(o.order_id) AS n FROM arrangement arr "
            "LEFT JOIN occupancy o ON o.arr_id=arr.arr_id "
            "WHERE arr.status='ACCEPTED' GROUP BY arr.arr_id"
        ):
            orders = self.conn.execute(
                "SELECT orders FROM arrangement WHERE arr_id=?", (r["arr_id"],)
            ).fetchone()
            if r["n"] != len(json.loads(orders["orders"])):
                errs.append(f"accepted arrangement {r['arr_id']} occupancy mismatch")
        for r in self.conn.execute(
            "SELECT point, cell_id, COUNT(*) AS n FROM occupancy "
            "GROUP BY point, cell_id HAVING n>1"
        ):
            errs.append(f"point {r['point']} cell {r['cell_id']} occupied {r['n']} times")
        for arr in self.arrangements():
            if arr["status"] != STATUS_ACCEPTED:
                continue
            try:
                self.recheck(arr["arr_id"])
            except CargoError as exc:
                errs.append(f"accepted arrangement {arr['arr_id']} unrecheckable: {exc}")
        return errs
