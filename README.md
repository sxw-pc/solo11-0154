# 危废交运闭环采证台

离线虚构危废批次的分类准运、持证联单双签责任移转、接收单位许可与地磅差量结案。三个子系统各有实体和状态，报表保留旧结论与争议责任；这里的结论是交运法定责任闭环，与冷链温控或仓门库存对账不同。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m wastetransit demo --db demo.sqlite
python -m wastetransit audit --db demo.sqlite
python -B -m pytest -q -p no:cacheprovider
```

独立入口 `waste-import`、`manifest-import`、`receipt-import` 使用 `--db state.sqlite --input fixtures/<waste|manifest|receipt>_ok.json`；`_bad.json` 分别复现错误分类、路线偏离和磅差超限。数据均为合成，真实许可/签章需外部验证，不在本版本能力范围。

## 残渣处理周期凭证（residue_trace）

接收点能复核母批磅单，但处置机台只交了一行“装入1、运行1、取出1”的计数。这三个计数对 `L→P→E`（残渣确实经历处理）与 `L→E→P`（处理在机台发生却没作用到残渣）两种现场历史完全不可区分，因此计数摘要永远不能单独签发可再转移的周期凭证。核心问题是：**运行是否实际发生在同一批实物仍留在机台的那段时间内。**

系统把三类原件离线采录后，按 (设备, 部件) 重构成同一份物理事件历程（带时序约束的有向图），后到事实只能收窄可能性或判冲突，绝不合成任何 L/P/E 动作：

| 原件 | 入口 | 证明力 |
| --- | --- | --- |
| `residue_cycle.json` 设备汇总（无交接时序） | `residue-import` | 仅计数；两种历史并存时报告不确定并展示阻止签证的现场操作顺序 |
| 独立保护控制器联锁签认 | `interlock-import` | 合同一：物理联锁本轮激活、装料箱绑定连续、真运行确认、取出时解锁，且旁路明确关闭（`bypass_state="off"`）才可签；无精确时刻也能签，`run_at` 留空。旁路状态 `on`/`unknown` 时“运行已完成”只证明裸机运行，仅配置联锁不能补证明 |
| 处理站原帧（装入/接续/离场见证 + 独立设备运行时标） | `station-import` | 合同二：约束图强制 `load→run→exit` 才可签并给出时序证明；运行与离场仍可对换且关键区间无法续采时不签 |

凭证状态：`cycle_certified`（可再转移）、`hold_origin`（证明成立但母批未闭合，阻断签发但不抹现场事实）、`uncertain_blocking`（运行/离场可对换，附全部可能现场顺序）、`sequence_conflict`（跨票锁状态等无任何可执行历史）、`pending_recollection`（已离场但证据不全，保留合成待查、不重做原过程）。母批复磅转入 `difference_investigation` 时既有凭证自动转 `hold_origin`，母批重新闭合后可恢复。

```powershell
python -m wastetransit residue-import   --db state.sqlite --input fixtures/residue_cycle.json
python -m wastetransit interlock-import --db state.sqlite --input fixtures/residue_interlock_ok.json
python -m wastetransit station-import   --db state.sqlite --input fixtures/residue_station_ok.json
python -m wastetransit cycle-query --db state.sqlite --batch WASTE-1
```

`cycle-query` 输出共同设备/部件历程（去重后的物理事件、时序约束边、`run_inside_residence` 三值结论与阻止签证的现场顺序）。所有原件经 `evidence` 哈希登记、命令经 `file_journal` 留痕，`audit` 可在空库逐文件重放并比对全部凭证结论。全部是本地合成 L/P/E 状态和任意时标，不涉及处置操作或真实仪器。

## 无 pytest 环境

应用代码仅依赖标准库。若镜像没有安装 pytest，`python -B -m pytest -q -p no:cacheprovider` 会落到仓库内 `pytest/` 引导包，用 `unittest` 运行同一套测试；已安装 pytest 时正常使用第三方 pytest，互不影响。
