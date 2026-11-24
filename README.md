# 危废交运闭环采证台

离线虚构危废批次的分类准运、持证联单双签责任移转、接收单位许可与地磅差量结案。三个子系统各有实体和状态，报表保留旧结论与争议责任；这里的结论是交运法定责任闭环，与冷链温控或仓门库存对账不同。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m wastetransit demo --db demo.sqlite
python -m wastetransit audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口 `waste-import`、`manifest-import`、`receipt-import` 使用 `--db state.sqlite --input fixtures/<waste|manifest|receipt>_ok.json`；`_bad.json` 分别复现错误分类、路线偏离和磅差超限。数据均为合成，真实许可/签章需外部验证，不在本版本能力范围。

## 有限窗口采集计划

`wastetransit/capture_plan.py` 是纯规划核心：主设备每个离散时刻只能读一件，同一组原件（如原帧与独立签认）必须出自同一次发布才算取得，目标是先保证可获得的有效证据组总权重。两种预先授权手续：发布边界 `lock` 锁定已出现的发布（下一次采样不再新发布，不得把未观测批次报为已观测）；独立接收机 `retention` 并行留存原帧但不停发布，落后一个版本的留存件不能与当前签认跨版配对。锁定回执丢失时，首条现场动作必须在“已锁定/已轮转”两种相容状态下都合法，先做 `mirror_check` 再按观测分支读取。

```powershell
python -m wastetransit.capture_sim run --input fixtures/capture_queue.json
python -m wastetransit.capture_sim run --input fixtures/capture_queue.json --offline --observe locked --ticks 0 1 2 3 --end 4
```

`wastetransit/capture_desk.py` 在原有差量驱动待采列表之上，于任何设备提交前保存计划（`capture_plan`/`capture_plan_step`），确认不可采的片段连同最后可读时刻留在 `missing_piece`，真实回执经 `submit_receipt` 入 `capture_receipt`；计划本身不替代真实字节，也不会重新核接收裁定或既有接收证书。规划行为的复现用例见 `tests/test_receiver_capture_plan.py`。

