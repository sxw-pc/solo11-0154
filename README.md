# 危废交运闭环采证台

离线虚构危废批次的分类准运、持证联单双签责任移转、接收单位许可与地磅差量结案。三个子系统各有实体和状态，报表保留旧结论与争议责任；这里的结论是交运法定责任闭环，与冷链温控或仓门库存对账不同。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m wastetransit demo --db demo.sqlite
python -m wastetransit audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口 `waste-import`、`manifest-import`、`receipt-import` 使用 `--db state.sqlite --input fixtures/<waste|manifest|receipt>_ok.json`；`_bad.json` 分别复现错误分类、路线偏离和磅差超限。数据均为合成，真实许可/签章需外部验证，不在本版本能力范围。

保留样复测凭据：`assay-import` 采录初筛（`fixtures/assay_mountings.json` 格式）、现场委托与复测动作，读数为无量纲合成值，不接实验室仪器；`assay-plan --batch WASTE-1 --budget 2` 显示当下仍相容的逐格结论、相互依赖、可兑现的复测动作与能完成三格的读数分支。声明 `assay_required` 的批次在保留样证据不足时挂起 `assay_hold`，读数超签认阈值进入 `assay_review`，两者下游均为 `classification_hold`；委托须在读取前固定，联合展开的成对观测只能成套引用，参考保留样未证实归还前不得借给新批。
