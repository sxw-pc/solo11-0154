# 危废交运闭环采证台

离线虚构危废批次的分类准运、持证联单双签责任移转、接收单位许可与地磅差量结案。三个子系统各有实体和状态，报表保留旧结论与争议责任；这里的结论是交运法定责任闭环，与冷链温控或仓门库存对账不同。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m wastetransit demo --db demo.sqlite
python -m wastetransit audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口 `waste-import`、`manifest-import`、`receipt-import` 使用 `--db state.sqlite --input fixtures/<waste|manifest|receipt>_ok.json`；`_bad.json` 分别复现错误分类、路线偏离和磅差超限。数据均为合成，真实许可/签章需外部验证，不在本版本能力范围。
