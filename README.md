# 挂车换接下的双向过磅与净装载事实仓

一台牵引车第一次带空挂车进厂、第二次却换了另一挂车出厂时，若按车牌配对两次
毛重，会把两套不同空车质量混在一起，制造不存在的净货重。本仓库把问题拆成
三个互相独立的模块：

* **车辆组合目录** (`weighbridge/catalog.py`)：只管车辆稳定身份、空车质量整数
  区间 `[lo, hi]`（kg）以及按严格递增序号发生的组合动作。它不知道磅单。
* **过磅事实仓** (`weighbridge/warehouse.py`)：消费目录在票面序号处的 *公开组合
  产品*，把毛重换算成货重区间、组织运输、计算净装卸量的精确区间、处理重评。
* **货舱交班** (`weighbridge/cargo.py`)：在磅单之上补一层货舱事实——挂车舱格的
  稳定身份、内衬与接触史、清洗证据、以及订货方按颜色/客户版本的整批匹配。

全部输入为本地合成整数记录，不连接地磅或车牌识别设备。Python 3.11，仅用标准
库（SQLite + `fractions.Fraction`）。

## 快速开始

```bash
python3 -m unittest discover -s tests -v     # 80 个测试
python3 -m demo.run_demo                     # 合成过磅案例
python3 -m demo.run_cargo                    # 合成货舱交班案例
python3 -m demo.run_demo --json              # 另写 demo/out/*.json
python3 -m demo.run_cargo --json             # 另写 demo/out/cargo_report.json
```

```python
from weighbridge import Catalog, Warehouse, LOAD, IN, OUT

c = Catalog()
c.define_mass("tractor", "T1", 7000, 7200)   # 整数 kg，lo <= hi
c.define_mass("trailer", "R1", 1500, 1600)
c.define_mass("trailer", "R2", 2000, 2200)

c.apply_attach("T1", "R1")        # seq 1
c.apply_detach("T1", "R1")        # seq 2
c.apply_attach("T1", "R2")        # seq 3
print(c.product_at(1, "T1").stable_identity())   # ('T1', 'R1')，历史不被换挂覆盖

w = Warehouse(c)
w.add_ticket(dict(ticket_id="IN",  tractor="T1", catalog_seq=1, ts=100,
                  direction=IN,  gross=9000))
w.add_ticket(dict(ticket_id="OUT", tractor="T1", catalog_seq=3, ts=200,
                  direction=OUT, gross=12000))

ev = w.preview(LOAD, "IN", "OUT")  # DRAFT：只算，不占票
print(ev.net_lo, ev.net_hi)        # 2300 2600（整数空重区间上的精确极值）
w.confirm(ev.trip_id)              # CONFIRMED：此刻才占票
print(w.reconcile())               # [] 表示核对通过
```

## 目录语义

* **稳定身份**：实体 ID 为 `tractor:<id>` / `trailer:<id>`，永不复用。
* **动作序号**：`attach` / `detach` / `set_mass` 的序号严格递增；失败不消耗
  序号。**批动作** `apply_batch` 在单事务内执行，任一步失败整批回滚（序号、
  绑定、产品、批次记录全部不存在）。
* **组合约束**：一个挂车同时至多一台牵引车；一台牵引车同时至多一个挂车。
  由两枚 `WHERE detach_seq IS NULL` 的部分唯一索引强制。
* **组合产品**：每次 `attach`/`detach` 产生一个 `EMPTY` 产品；`set_mass` 产生
  `RELABEL` 产品。产品带 `relation_src`（`initial`/`attach`/`detach`/
  `mass_correction`）、`attach_seq`、`predecessor`。
* **历史不被覆盖**：产品的质量是落盘时的快照。查询任意序号：

  ```python
  c.product_at(seq, tractor)   # 该序号处已生效的 live 产品
  c.history("trailer", "R1")   # 该实体参与过的全部产品（含更正版）
  c.snapshot_at(seq, tractor)  # 可跨系统交换的 JSON 快照
  c.corrected_product(p)       # 同一挂接 + attach_seq 的最新更正版
  ```

  换挂之后，旧序号处的旧产品原样保留；`corrected_product` 只沿同一
  `(identity, attach_seq)` 的 `predecessor` 链走，绝不从 "当前挂接" 猜历史。

## 仓库语义

**单票货重**：`tare` 为组合产品各实体空车质量之和，
`goods = gross - tare`，可行区间
`[max(0, gross - tare_hi), gross - tare_lo]`；`gross < tare_lo`（上界为负）时该票
不合物理约束。`explain_ticket(tid)` 给出毛重、`tare`、`goods`、每个实体区间与
宽度——即 "这张票为什么是这个货重、哪些实体贡献了不确定性"。

**运输**：`preview(kind, ticket_in, ticket_out)` 显式选定一 IN 一 OUT 票，绝不按
时间就近猜配。IN 时间不得晚于 OUT；两票必须同车；一票不能同时占两个角色。
结果状态：

| 状态 | 含义 |
| --- | --- |
| `DRAFT` | 预览，未占票；失败时为不可确认的草稿 |
| `CONFIRMED` | 已确认；两票写进 `confirmed_ticket`，此后不可被别的运输占用 |
| `REVIEW` | 对已确认运输的重开/重评 |
| `SUPERSEDED` | 未确认的旧草稿/复核被同族新结果取代 |

**净装卸量**：

* 组合完全相同（同一牵引车 + 同一挂车）：空车质量是同一未知量，在毛重差中
  **精确抵消**，`net == |gross_out - gross_in|`，与区间宽度无关。
* 换挂：共享实体（如牵引车）系数为 0，精确抵消；不同实体以目录区间参与。
  在 "两张票货重非负 + 运输方向" 的联合可行域上，对净装卸量做**精确整数分支
  定界**（实数 LP 松弛用有理数顶点枚举），输出最小/最大净装卸量与货流区间。
  可行域为空则拒绝，区分 `DIRECTION_IMPOSSIBLE` 与 `TICKET_INFEASIBLE`。
* 绝不使用区间中点或实数顶点作为结算值。

`contributions` 逐个实体给出 `role`（`cancelled`/`fixed`/`uncertain`）、线性系数与
区间宽度。

**重评与重开**：

```python
c.apply_set_mass("trailer", "R1", 1500, 1600)  # 目录迟到更正
re = w.reopen(trip_id)        # 新增 REVIEW 运输，带 predecessor_trip
w.evaluation_history(re.trip_id)  # 旧（CONFIRMED）与新（REVIEW）都在，链式可追溯
```

* 已确认运输及其评估是历史事实，不会被覆盖或标为 `superseded`。
* 被确认运输占用的票不会被重新记成另一运输（重评沿用同一票与角色）。
* 重评默认对原评估中的 *历史产品* 取 `corrected_product`，而不是套用当前挂接。

**外部组合快照**：`Warehouse(None)` 可完全不依赖目录运行；用
`register_snapshot(...)` 导入与目录相同 JSON 格式的快照，按票面序号取产品。
若某张票的序号没有对应快照，直接报错——绝不回退到当前挂接去猜。

**核对**：`w.reconcile()`（`w.verify()` 同义，含目录自检）返回不一致列表，覆盖：
确认运输的两票方向/同车/时间序与可行性、确认占票唯一、每条评估在存下的快照
上可重算、同身份精确抵消、重评前序存在。

## 货舱交班

货舱层解决 "货舱清洗凭证能否覆盖下一票已订的货种"。它为挂车登记具有稳定身份
的舱格，按已确认运输记录放入的纸粒颜色，靠清洗证据判断旧接触是否真的消除，
再按订货方的颜色规则做整批匹配。车场与订单各用一个 SQLite 库，可分别关闭、
重开、离线复查。

```python
from weighbridge import Yard, OrderBook, Warehouse

y = Yard(warehouse=w)                        # w 是已确认运输所在的过磅仓
A = y.add_cell("R1", "A", ts=0)              # 稳定身份 CELL0001
B = y.add_cell("R1", "B", ts=0)              # 稳定身份 CELL0002
y.load_from_trip(trip_id, "blue", cell_id=A) # 票面组合 -> 挂车 -> 舱格 A
y.load_cargo(B, None, ts=1100)               # 空跑：不新增颜色

y.rename(A, "A1", ts=1150)                   # 只改显示，接触史不变
y.clean(A, done_ts=1250, steps=[             # 逐道工序声明可清除颜色
    {"step": "干刷", "clears": []},
    {"step": "蓝专用溶剂冲洗", "clears": ["blue"]},
])
y.handover(1300)                             # 每个舱格在交班点的剩余颜色+来源
y.residual_colors(A, 1300)                   # 蓝色已清零；来源链可追溯

ob = OrderBook(yard=y)
ob.set_ban("white", "white", ["blue"])       # 客户版本 white 的白单禁蓝
ob.add_order("W", "white", "white", point=1300, allowed_labels=["A1", "B"])
ob.add_order("N", "normal", "normal", point=1300, allowed_labels=["B"])
ob.solve_point(1300)                         # 整批匹配；无解给 Hall 阻碍
arr = ob.preview(["W", "N"], point=1300)     # 预审：不占格
ob.accept(arr["arr_id"])                     # 正式接受：冻结凭证、占用格
ob.recheck(arr["arr_id"])                    # 按原版本 + 原清洗凭证复查
```

**舱格身份与内衬**

* `cell_id` 是稳定身份，格号 (label) 只是显示；`rename` 只加一条显示事件，
  接触史与内衬都不变。
* `reline` 创建新内衬，新内衬的接触史从空开始；旧内衬的接触仍在历史里，可在
  更早的交班点看到，但不再计入当前剩余颜色。

**接触与清洗证据**

* 接触只来自 `warehouse` 中为 `CONFIRMED` 的运输；`load_from_trip` 从 OUT 票的
  组合产品取挂车，再落到具体舱格（挂车有多个舱格时须显式 `cell_id`）。
* 空跑 (`color=None`) 不新增颜色。已排空的颜色仍留在接触史，只有清洗证据才把
  它从剩余颜色中移除。
* 清洗单按 *舱格稳定身份 + 内衬 + 完成时间* 认定：只有同一内衬、完成时间不早
  于该接触、且颜色被某道工序覆盖的旧接触才消除。收据写同一格号却落在另一挂车
  的舱格上（`cell_hint` 或 `trailer` 不符）时，直接拒绝，不构成证据。
* `residual_colors(cell, at)` 给出该交班点剩余颜色、最早未消除接触时间、接触
  id 与来源（trip / 内衬 / 来源类型）。

**整批匹配与安排**

* `candidates(point)` 逐单给候选，`solve_point(point)` 对整批做二分图最大匹配；
  逐单都有候选但整批无解时（Hall 阻碍），`impediments` 列出阻碍集与逐单原因
  （`NO_ELIGIBLE_CELL` / `CONTENTION`）。
* 一批订单必须在同一交班点分给互不相同的舱格，每单只占一格；订单可限定允许
  格号；不同交班点可复用同一格。
* `preview(...)` 只预审、不占格，多份预审可并存；`accept(...)` 重新校验后占格，
  由 `(point, order)` 与 `(point, cell)` 唯一约束保证并行接受时不会重复占格。

**冻结复查**

* `accept` 时冻结客户版本的禁配集合与当时的清洗凭证（内衬、剩余颜色、清洗
  单）。`recheck` 只读冻结内容，因此之后追加禁配、换内衬、补登记新接触都不会
  改变这次复查结论。
* `live_status` 则对照 *当前* 车场状态重新核验，用于 "曾经接受、如今车场已变"
  的下游判断，两者的差异被明确区分。

`y.reconcile()` / `ob.reconcile()` 分别自检：舱格有显示与初始内衬、接触/清洗
落在其舱格的内衬上、已接受安排的占格数正确、任意点每格至多一份占用、所有已
接受安排可复查。

## 容量

目录 100 实体（`define_mass` 时强制）；仓库 1000 张票、200 次运输；车场 200
舱格；订单 500 张、安排 500 份。同一车同一秒可有多张不同 `ticket_id` 的票。

## 文件

```
weighbridge/catalog.py      独立车辆组合目录
weighbridge/warehouse.py    过磅事实仓 + 精确整数求解
weighbridge/cargo.py        货舱交班：舱格/内衬/清洗证据/整批匹配
weighbridge/__init__.py     公共 API
tests/test_weighbridge.py   49 个测试（含小整数区间暴力核对极值）
tests/test_cargo.py         31 个货舱交班测试
demo/run_demo.py            可复跑合成过磅案例
demo/run_cargo.py           可复跑合成货舱交班案例
demo/out/                   演示输出（--json 时生成）
```

## 覆盖的案例

* 同车同挂精确抵消（含极宽空车区间）
* 仅换挂：共享牵引车抵消、两挂车参与整数区间极值（与暴力枚举逐个核对）
* 目录迟到更正：已确认运输不变，新增带前序身份的重评
* 方向不可能、单票物理不可行、两票区间不相交
* 同票竞争：已确认运输占票后另一运输确认失败，且失败不改变状态/不占票
* 重开：多轮更正形成可追溯评估链
* 外部快照独立运行、快照冲突拒绝、缺失历史产品拒绝
* 批动作原子回滚、序号严格递增、单挂车/单牵引车约束、容量上限
* 货舱：改号只改显示、内衬换代清空接触史、逐道工序覆盖、跨挂车同格号证据被拒、
  空跑不新增颜色
* 逐单有候选却整批无解（同车 A/B 例子），补覆盖蓝色的清洗单后可行
* 固定未洗状态、客户允许接触蓝色时也可行
* 预审多份、并行抢同一格时正式接受互斥、跨交班点复用
* 冻结复查按原版本与原清洗凭证；日结后离线、两库分别重开仍可复查

