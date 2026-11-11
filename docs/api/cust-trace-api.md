# 保险核心跟踪（cust-trace）接口文档

## 一、文档信息

| 项目 | 内容 |
|---|---|
| 需求名称 | 保险核心跟踪 |
| 接口前缀 | `/api/agent/workspace/cust-trace` |
| 技术栈 | Java 8 / Spring Boot / MyBatis |
| 鉴权方式 | 与现有工作台接口一致（Cookie / 网关统一鉴权） |
| 通用返回 | `Result<T>`：`{ "code": "200", "message": "...", "data": ... }`，业务失败返回非 200 code |

## 二、通用约定

### 1. 请求/响应格式
- 全部接口：`POST` + `Content-Type: application/json`
- 入参字段均为**驼峰命名**
- 统一返回 `Result<T>`，成功 `code="200"`；业务异常 `code="400/404/409"` 并附 `message`；参数校验失败返回 `code="400"`（`MethodArgumentNotValidException` 已统一映射）

### 2. 一级分行换算规则
- 入参 `bbkId` 为 **二级分行** 机构号，落库/查询前经 `WorkspaceSkillConfigService#getFirstBbk` 换算为 **一级分行** 后使用。
- 换算结果仅用于内部落库/查询条件，不写回请求对象；`skippedKeys` 中的 bbkId 为请求原文（二级）。
- 无法换算时按现有兜底逻辑返回原值。

### 3. 唯一性规则
- 主表唯一索引：`(sap_id, bbk_id, cust_uid, deleted, deleted_at)` 五字段。
- `deleted=0` 时 `deleted_at=NULL`，唯一性由应用层"先查后写"保证（并发风险已接受，不做锁）。
- 同一键存在 `deleted=1` 历史时，再次新增直接插入新行（保留历史，不复活旧行）。

### 4. 数据库兼容性
- 需要 MySQL 5.7+ / TDSQL（支持行值构造器 `(a,b) IN ((..),(..))` 语法）。

---

## 三、数据结构

### 1. 主记录 CustTrace（数据字典）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| id | Long | - | 主键（新增后返回） |
| sapId | String | 新增必填 | 客户经理 ID |
| bbkId | String | 新增必填 | 客户经理二级分行（入参，落库为一级分行）；查询返回为一级分行 |
| custBbkOrgId | String | 否 | 客户归属一级分行机构号（**前端直传、不做换算**；新增时传，查询返回；编辑锁定不可改） |
| custUid | String | 新增必填 | 客户 UID |
| custName | String | 否 | 客户姓名 |
| aum | String | 否 | 客户 AUM |
| filename | String | 否 | 经营方案地址；新增时从原客户清单直传，查询时返回（前端需求补充） |
| skillId | String | 新增必填 | 客户技能 ID（编辑锁定） |
| skillName | String | 否 | 客户技能名称 |
| bxPlan | String | 否 | 保险计划 |
| touched | Integer | 否 | 触达标记：0-未触达 1-已触达（默认 0） |
| custIntention | Integer | 否 | 客户意向：0-待了解 1-低意向 2-中意向 3-高意向（默认 0） |
| createdTime | String | - | 创建时间 |
| touchList | List | - | 接触计划列表（type=0，按 touchDate 降序） |
| traceList | List | - | 跟进记录列表（type=1，按 traceTime 降序） |

### 2. 接触/跟进计划 CustTraceExt

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| id | Long | 更新/删除必填 | ext 主键 |
| traceId | Long | 新增必填 | 关联主表 id |
| type | Integer | 新增/更新必填 | 0-接触计划；1-跟进记录 |
| touchPlan / touchType / touchDate | - | type=0 组 | 接触计划内容 / 接触方式(0-企微 1-招乎 2-电访 3-面访) / 接触日期(yyyy-MM-dd) |
| tracePlan / traceSkillId / traceSkillName / traceType / traceTime | - | type=1 组 | 跟进计划 / 跟进技能ID / 跟进技能名称 / 跟进方式 / 跟进时间 |
| touched | Integer | type=1 必填 | 触达结果 0/1，回写主表 touched |

> **字段组互斥**：type=0 仅 touch_* 组可填，type=1 仅 trace_* 组可填；另一组字段会被清空/置 NULL。

### 3. 分组返回 CustTraceGroup

| 字段 | 说明 |
|---|---|
| sapId | 客户经理 ID |
| custTraceList | 该客户经理下的主记录列表（含 touchList/traceList） |

### 4. 批量新增结果 CustTraceBatchResult

| 字段 | 说明 |
|---|---|
| successCount | 成功插入条数 |
| skipCount | 因客户已存在而跳过的条数 |
| skippedKeys | 跳过的键列表（格式：`sapId-bbkId(二级)-custUid`） |

---

## 四、接口列表

| # | 接口 | 说明 |
|---|---|---|
| 1 | POST /batch-create | 批量新增跟踪客户（可携带接触/跟进计划） |
| 2 | POST /batch-delete | 批量逻辑删除跟踪客户（级联删除关联计划） |
| 3 | POST /update | 编辑主表（非锁定字段） |
| 4 | POST /query | 按 (sapId, bbkId) 组合批量查询（按 sapId 分组） |
| 5 | POST /query-by-bbk | 按分行查询（按客户经理分组） |
| 6 | POST /ext/add | 新增单个接触计划/跟进记录 |
| 7 | POST /ext/update | 编辑单个接触/跟进计划 |
| 8 | POST /ext/delete | 按 id 逻辑删除单个计划 |

---

## 五、接口明细

### 1. 批量新增跟踪客户

- **路径**：`POST /api/agent/workspace/cust-trace/batch-create`
- **功能**：批量加入跟踪客户；元素可携带 extList（接触/跟进计划）；主从同事务写入；同键（sapId + 一级bbkId + custUid + deleted=0）已存在的元素跳过
- **请求体**：

```json
{
  "items": [
    {
      "sapId": "客户经理ID",
      "bbkId": "二级分行机构号",
      "custBbkOrgId": "客户归属一级分行机构号",
      "custUid": "客户UID",
      "custName": "客户姓名",
      "aum": "AUM",
      "filename": "经营方案完整URL",
      "skillId": "技能ID",
      "skillName": "技能名称",
      "bxPlan": "保险计划",
      "touched": 0,
      "custIntention": 0,
      "extList": [
        {
          "type": 0,
          "touchPlan": "接触计划",
          "touchType": 0,
          "touchDate": "2026-11-05"
        },
        {
          "type": 1,
          "tracePlan": "跟进计划",
          "traceSkillId": "技能",
          "traceType": 1,
          "traceTime": "2026-11-05 10:00:00",
          "touched": 1
        }
      ]
    }
  ]
}
```

- **校验**：元素中 `sapId`、`bbkId`、`custUid`、`skillId` 必填；extList 中 type=0/1 的字段组互斥
- **返回 data**：`CustTraceBatchResult`

```json
{
  "code": "200",
  "data": {
    "successCount": 1,
    "skipCount": 0,
    "skippedKeys": []
  }
}
```

- **边界**：同键重复元素跳过并计数；deleted=1 历史插新行；空 items 返回全 0

### 2. 批量删除跟踪客户

- **路径**：`POST /api/agent/workspace/cust-trace/batch-delete`
- **功能**：按 (sapId, bbkId, custUid) 列表批量逻辑删除；主表 `deleted 0→1 + deleted_at(ms)`；关联 ext 级联逻辑删除；同事务
- **请求体**：

```json
{
  "items": [
    { "sapId": "客户经理ID", "bbkId": "二级分行机构号", "custUid": "客户UID" }
  ]
}
```

- **校验**：元素 `sapId`、`bbkId`、`custUid` 必填
- **返回 data**：`null`
- **边界**：删除不存在的键返回成功且不产生脏数据；已删除记录不会重复删除

### 3. 编辑跟踪客户主表

- **路径**：`POST /api/agent/workspace/cust-trace/update`
- **功能**：按主表 id 编辑非锁定字段
- **请求体**：

```json
{
  "id": 1,
  "custName": "客户姓名",
  "aum": "AUM",
  "skillName": "技能名称",
  "bxPlan": "保险计划",
  "touched": 1,
  "custIntention": 2
}
```

- **校验**：`id` 必填；id 定位不到 deleted=0 记录返回 404
- **锁定字段（不可修改）**：`sapId`、`bbkId`、`custUid`、`skillId`、`custBbkOrgId`——请求体不含这些字段（传入也不生效）
- **返回 data**：`null`
- **边界**：无可更新字段时跳过更新（不报错）；更新只作用于 deleted=0 记录；null 字段表示"不修改"

### 4. 按组合查询跟踪客户

- **路径**：`POST /api/agent/workspace/cust-trace/query`
- **功能**：按 (sapId, bbkId) 组合列表批量查询未删除记录，按 sapId 分组（分组顺序与入参首现顺序一致）；单查/批查复用同一接口
- **请求体**：

```json
{
  "items": [
    { "sapId": "客户经理ID", "bbkId": "二级分行机构号" }
  ]
}
```

- **校验**：元素 `sapId`、`bbkId` 必填
- **返回 data**：`List<CustTraceGroup>`，每条主记录含 touchList/traceList（分别按 touchDate / traceTime 降序）

```json
{
  "code": "200",
  "data": [
    {
      "sapId": "客户经理ID",
      "custTraceList": [
        {
          "id": 1,
          "sapId": "客户经理ID",
          "bbkId": "一级分行",
          "custBbkOrgId": "客户归属一级分行",
          "custUid": "客户UID",
          "custName": "姓名",
          "aum": "AUM",
          "filename": "经营方案完整URL",
          "skillId": "技能ID",
          "skillName": "技能名称",
          "bxPlan": "计划",
          "touched": 1,
          "custIntention": 2,
          "createdTime": "2026-11-05 10:00:00",
          "touchList": [
            { "id": 10, "traceId": 1, "type": 0, "touchPlan": "计划", "touchType": 0, "touchDate": "2026-11-05" }
          ],
          "traceList": [
            { "id": 11, "traceId": 1, "type": 1, "tracePlan": "跟进", "traceType": 1, "traceTime": "2026-11-05 10:00:00" }
          ]
        }
      ]
    }
  ]
}
```

- **边界**：无命中返回空列表；仅返回 deleted=0 记录

### 5. 按分行查询跟踪客户

- **路径**：`POST /api/agent/workspace/cust-trace/query-by-bbk`
- **功能**：按二级分行（换算一级）查询该分行下所有客户经理的未删除跟踪客户，按 sapId 分组
- **请求体**：

```json
{ "bbkId": "二级分行机构号" }
```

- **校验**：`bbkId` 必填
- **返回 data**：`List<CustTraceGroup>`（同接口 4，含 touchList/traceList）
- **边界**：该分行无未删除记录返回空列表

### 6. 新增单个接触/跟进计划

- **路径**：`POST /api/agent/workspace/cust-trace/ext/add`
- **功能**：为指定主记录新增单个接触计划(type=0)或跟进记录(type=1)；type=1 时同事务回写主表 `touched`
- **请求体**：

```json
{
  "traceId": 1,
  "type": 1,
  "tracePlan": "跟进计划",
  "traceSkillId": "技能ID",
  "traceSkillName": "技能名称",
  "traceType": 0,
  "traceTime": "2026-11-05 10:00:00",
  "touched": 1
}
```

- **校验**：`traceId`、`type` 必填；type=0/1 字段组互斥；**type=1 时 `touched` 必填且为 0/1**；主记录不存在返回 404
- **返回 data**：`null`
- **边界**：`custIntention` 不自动更新；type=0 可不传 touched

### 7. 编辑单个接触/跟进计划

- **路径**：`POST /api/agent/workspace/cust-trace/ext/update`
- **功能**：按 ext id 编辑单个计划；按**更新后 type** 判定是否回写主表 touched（type=1 回写）；另一组字段置 NULL（字段组互斥兜底）
- **请求体**：

```json
{
  "id": 10,
  "type": 1,
  "tracePlan": "更新后跟进计划",
  "traceType": 2,
  "traceTime": "2026-11-06 09:00:00",
  "touched": 0
}
```

- **校验**：`id`、`type` 必填；ext 不存在或已删除返回 404；type=1 时 `touched` 必填且为 0/1
- **返回 data**：`null`
- **边界**：仅更新 deleted=0 的 ext；type 变更（0↔1）时旧字段组被清空；回写目标以 ext 已关联的主记录为准

### 8. 删除单个接触/跟进计划

- **路径**：`POST /api/agent/workspace/cust-trace/ext/delete`
- **功能**：按 ext id 逻辑删除单个计划（deleted 0→1）
- **请求体**：

```json
{ "id": 10 }
```

- **校验**：`id` 必填
- **返回 data**：`null`
- **边界**：删除 type=1 跟进记录时**不回写**主表 touched；仅 deleted=0 记录可删；删除不存在 id 影响 0 行视为成功

---

## 六、错误码约定

| code | 场景 |
|---|---|
| 200 | 成功 |
| 400 | 参数校验失败（必填缺失/枚举值非法/touched 必填等） |
| 400 | 业务参数错误（如 type 必须为 0 或 1） |
| 404 | 记录不存在或已删除（编辑主表 / ext 操作目标不存在） |
| 409 | 业务冲突（预留，当前未启用） |

> 统一返回结构：`{ "code": "...", "message": "...", "data": null }`；参数校验失败 message 为 `字段: 校验消息` 的拼接。

## 七、与前端对接注意项

1. 入参全驼峰；`bbkId` 传二级分行（接口内部换算一级）；
2. `custBbkOrgId` 客户归属一级分行**前端直传、不做换算**，仅新增时传、查询返回、编辑不可改；
3. `skippedKeys` 中的 bbkId 为请求原文（二级）；
4. `query`/`query-by-bbk` 返回按 sapId 分组，每组内每条主记录带独立的 `touchList`（接触计划，按 touchDate 降序）与 `traceList`（跟进记录，按 traceTime 降序）；
5. `touched` 与 `custIntention`：主表新增默认 0；type=1 跟进记录新增/编辑会回写主表 `touched`，但 `custIntention` 永远手动维护（仅主表编辑可改）；
6. 建议批量新增/删除单次元素数 ≤ 200（避免 SQL 参数过多），当前无硬性限制。
