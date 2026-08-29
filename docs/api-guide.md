# 审核 API 对接

## 1. 端点与鉴权

`POST http://<host>:8000/v1/audit` —— 审核入口。

| 项 | 说明 |
|---|---|
| 鉴权 | 请求头 `X-Api-Key: <API Key>`，或 `Authorization: Bearer <API Key>`（两者任选其一；无 Key / 禁用 → 401 `{"error":"invalid api key"}`） |
| 限流 | 每 Key 滑动窗口默认 **60 次 / 60 秒**（环境变量 `SAFEFUSION_RATE_LIMIT` 整体调整；超限 → 429 `{"error":"请求过于频繁"}`） |
| API Key 来源 | 管理面板「密钥管理」页创建（完整 Key 仅创建时显示一次）；standard / full 两组 |
| 分级差异 | `standard` 组：`detail` 为 `null`（只返回基本判定）；`full` 组：返回完整分层明细（关键词/正则/语义 Top5/白名单/LLM/黑白三值） |
| overrides | 请求级阈值覆盖**仅 full 组可用**，standard 携带 → 403 `{"error":"overrides 仅 full 组可用"}` |

## 2. 请求体（AuditRequest）

```jsonc
{
  "text": "待检测文本（可为 null，纯图片请求）",
  "images": [
    { "base64": "<base64 编码的图片数据>" },   // base64 / url 二选一
    { "url": "https://example.com/x.jpg" }     // http(s) 图片 URL
  ],
  "context": "可选上下文（如发帖场景/平台），用于 LLM 判定与缓存隔离",
  "skip_llm": false,                            // true = 跳过 LLM 兜底层（强制回退语义层）
  "overrides": {                                // 仅 full 组可用（全部可选）
    "semantic_threshold": 0.7,
    "margin_w": 0.05,
    "confidence_low": 0.35,
    "confidence_high": 0.75
  }
}
```

图片白名单、动图抽帧（GIF 均匀抽帧，任一帧违规 → 整体违规）等能力对 images 自动生效。

## 3. 响应体（AuditResult）

```jsonc
{
  "request_id": "uuid",                 // 请求唯一标识
  "timestamp": "ISO 8601",              // 完成时间
  "has_violation": true,                // 是否判定违规
  "confidence": 0.83,                   // 综合置信度 0~1
  "category": "色情",                    // 违规类别（违规时给出）
  "source": "semantic",                 // 判定来源（basic_rules_pass / keyword / light_model / semantic / llm ...）
  "cache_hit": false,                   // 是否命中缓存直接返回
  "detail": {                           // 仅 full 组；standard 组为 null
    "keyword":      { "hits": [...], "regex_filtered": [...] },   // 关键词命中 + 正则豁免
    "light_model":  { "label": "违规", "score": 0.9, "violation": true },
    "image_whitelist": [ { "frame": 0, "hit": true, "distance": 2 } ],
    "semantic":     { "black_top": [...], "black_avg": 0.8, "white_avg": 0.3, "margin": 0.5 },
    "llm":          { "is_violation": true, "category": "色情", "confidence": 0.9, "reason": "..." }
  }
}
```

异常响应统一为 `{"error": "<中文/脱敏文案>"}`（400 校验 / 401 鉴权 / 403 overrides / 429 限流 / 500 内部错误，脱敏不回栈）。

## 4. 示例

```bash
# curl（Key 请替换为「密钥管理」页创建的真实 Key——示例用占位符）
curl -X POST http://127.0.0.1:8000/v1/audit \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: sf_<你的API-KEY>" \
  -d '{"text": "抽到 2000 元话费，加群 xxxxxx 领奖"}'
```

```python
# Python（httpx；项目约定不使用 requests）
import httpx

resp = httpx.post(
    "http://127.0.0.1:8000/v1/audit",
    headers={"X-Api-Key": "sf_<你的API-KEY>"},
    json={"text": "抽到 2000 元话费，加群 xxxxxx 领奖"},
    timeout=30,
)
print(resp.json())
```

管理端快速验证：试运行页（`POST /admin/test-audit`）走管理令牌、等价 full 权限并返回完整 detail，无需创建 API Key。
