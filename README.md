# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 结构

资料（本 README）、规则（`app.py`，仅标准库）、数据（SQLite 表）、页面（`static/index.html`，原生 HTML/CSS/JS）分开维护，不引入任何第三方依赖。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/evaluations/handovers`：监督员指定离场专家的接手人（必须写明交接原因）
- `POST /api/evaluations/correct`：接手人提交更正分值（旧值/新值/时间留痕，原记录不可改）
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：按有效分值锁定评分轮次并保存排名快照（历史快照追加保存）

## 评分更正规则

- 原 `evaluations` 记录只可查看，不提供修改、删除入口；每次更正向 `score_corrections` 追加一条不可变记录，保留旧原始值/旧得分、新原始值/新得分、离场专家、接手人、交接原因和处理时间。
- 只有 `supervisor` 能指定接手人（`POST /api/evaluations/handovers`），必须填写交接原因；接手人不能是离场专家本人，已在本轮评分的专家不能再接手。
- 只有被指定的接手人（`evaluator`）能提交更正，且必须一次性更正该专家在该投标上的全部评分项；同一评分项只能更正一次。
- 有效分值 = 原分值，若存在更正则以更正后的新分值为准。当前排名、授标快照都按有效分值重算；授标快照追加写入 `ranking_snapshots`，历史排名可追查。
- 项目详情（`GET /api/tenders/{id}`，监督员/采购/审计）列出有效分值（标注原始/更正来源）、更正记录、专家交接、实时排名和历史快照；公开身份看不到更正明细。
- 授标前置条件：所有有效投标完成全部评分、没有未处理投诉、不存在交接原因缺失的交接或更正；评分锁定（已授标）后不能再更正。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、评分更正留痕与授标拦截和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
