# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评、评分更正交接和授标快照服务。

## 分层维护（互不耦合、无第三方依赖）

- `README.md`：资料说明
- `rules.py`：评分规则（纯函数）——录入值换算、原记录与更正链归并为有效分值、加权排名；不碰数据库
- `app.py`：数据与服务——SQLite 表结构、业务流程、HTTP 接口，只调用 `rules.py` 取计算结果
- `static/index.html`：页面——只读展示，列表与项目详情（有效分值、更正记录、历史排名、授标快照）

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/score-handoffs`：监督员指定接手人（须写明交接原因）
- `POST /api/evaluations/correct`：指定接手人凭交接授权提交更正分值
- `GET /api/tenders/{id}`：开标后返回 `scoring`（有效分值、更正记录、交接记录、当前排名、历史排名快照、授标快照）
- `POST /api/tenders/award`：按有效分值锁定评分轮次并保存排名快照

## 评分更正规则（专家离场/账号交接）

1. 原评分记录只读：`evaluations` 永不更新、不覆盖，谁都无法改旧值。
2. 监督员（`supervisor`）先调用交接接口指定接手人并写明原因；原因缺失不能提交，接手人不能与原评审人相同。
3. 只有被指定的接手人（`evaluator` 角色）能提交更正；接手人若与供应商有利益冲突同样被拦截。
4. 每次更正在 `score_corrections` 留痕：旧录入/旧得分、新录入/新得分、原评审人、接手人、交接原因、授权监督员、处理时间。
5. 交接授权按"当前有效持有人"校验并消费：接手人再离场时，监督员可按接手人继续指定，形成可追查的交接链。
6. 更正后立即按有效分值重算本轮排名，并写入 `ranking_snapshots`；授标时再固化一版快照，历史排名随时可追查。
7. 授标拦截：评分未完成、存在 `open` 投诉、交接/更正原因缺失，任一不满足都不能授标；授标后评分锁定，不能再更正或交接。
8. 多个评审席位对同一评分项取平均后再加权；被更正席位以最新更正值为有效值。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、角色权限，以及评分更正交接、有效分值重算、历史快照和授标拦截。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
