# 运行手册 stockflow

## 启动

```bash
docker compose up -d --build
curl -s localhost:18207/api/health
```

首页 `http://localhost:18207/` 打开后点「重跑」执行样例主流程；所有页面共用同一批次。

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

`down -v` 删除命名卷 `stockflow-data`；启动后第一次运行生成新库。

## 主流程

```bash
curl -s -X POST localhost:18207/api/demo/run | python3 -m json.tool | head -40
curl -s localhost:18207/api/quarantine.csv | head -5
curl -s localhost:18207/api/replay -X POST
curl -s localhost:18207/api/cost | python3 -m json.tool | head -30
curl -s localhost:18207/api/reconcile | python3 -m json.tool | head -40
curl -s localhost:18207/api/export/manifest.json
```

## 导入自己的文件

```bash
curl -s -F sample=movements_clean.csv localhost:18207/api/movements/import
curl -s -F file=@/path/to/movements.csv localhost:18207/api/movements/import
curl -s -F file=@/path/to/opening.csv localhost:18207/api/movements/opening
```

导入只接受与样例同列名（或登记过的英文别名）的文件；列名不对时首行会被判表头缺失。

## 增量补丁（上游每天只回传变化行）

```bash
curl -s -F file=@/path/to/patch.csv localhost:18207/api/movements/patch
curl -s -F sample=patch_day1.csv localhost:18207/api/movements/patch
curl -s localhost:18207/api/movements/patches            # 补丁历史
curl -s localhost:18207/api/movements/patches/<run_id>   # 完整采信报告
```

- 补丁与当前基线按 `(单据号, 行号)` 合并后走同一后半链，当天即可核算，不用等全量。
- 响应里的 `patch.adopted` 给出逐行分类（新增/升级/冲突/过旧/一致/无效/修复），
  `patch.rejudged` 列出因此改判的单据（如缺边调拨补齐后转采纳）。
- 同一补丁重试返回同一 `run_id`，不新增流水行或成本层；采信中断后旧批次与基线原样保留。
- 整批导入（`/api/movements/import`）会重置基线，适合全量重导；补丁批次之后
  导入新期初会自动沿用合并集，不需要补丁文件落盘。

## 盘点提交与版本

```bash
curl -s -X POST localhost:18207/api/reconcile/counts \
  -H 'Content-Type: application/json' \
  -d '{"count_no":"PD-C-05","revision":2,"rows":[{"sku":"000123","warehouse":"WH-A","counted_qty":"500","unit":"kg"}]}'
```

- 旧版本返回 409 `E_STALE_COUNT_REVISION`；同版本不同内容返回 409 `E_COUNT_REVISION_CONFLICT`。
- 同版本同内容幂等，重复提交不新增盘点行。
- 盘亏超过在手返回记录状态 `COUNT_SHORT`，不做负库存。

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 首页 200 但数字全 0 | 还没跑批次 | 点「重跑」或 POST `/api/demo/run` |
| 导入回报 404 | 样例名不在 `sample_data/` | 用上传文件或核对样例名 |
| 补丁回报 400 `E_PATCH_EMPTY` | 补丁没有数据行（空文件/仅表头） | 核对上游导出，基线与批次未改动 |
| `POST /api/replay` 409 | 老批次没有输入存档且源文件已移除 | 重新导入流水 |
| 盘点提交 409 | 版本旧于已记录版本 | 升版本号再提交 |
| 层金额与期末金额不符 | 有人直接改库 | 重跑批次会先删后插，恢复一致 |
