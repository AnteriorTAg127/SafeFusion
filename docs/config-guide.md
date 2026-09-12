# 配置体系

## 1. 四层优先级与来源标识

v0.3.0 配置存储升级为 **SQLite `settings` 表**（取代 v0.2.1 的 `data/config_overrides.json`），优先级：

```text
内置默认  <  config.yaml  <  数据库 settings（管理端在线设置）  <  环境变量
```

- **环境变量（最高优先）只覆盖内存、绝不写数据库**：`SAFEFUSION_<路径>_<键>` 钉住的叶子在合并时跳过 DB 值（例：`SAFEFUSION_THRESHOLDS_SEMANTIC_THRESHOLD=0.7`）；
- **密钥类只走环境变量**：`SAFEFUSION_LLM_API_KEY`（或 `OPENAI_API_KEY`）、`SAFEFUSION_EMBEDDING_API_KEY`；YAML 里的 `api_key` 键会被忽略并告警，管理端写库一律 422 拒绝，GET 响应只见「环境变量名 + 已配置布尔」；
- **来源徽标**：设置页每个字段旁有小徽标（默认=淡化虚线框 / YAML=灰 / 数据库=蓝 / 环境变量=橙），数据来自 `GET /admin/config/sources`——「当前生效值来自哪一层」一目了然；
- **12 个配置分组**：server / thresholds / embedding / llm / cache / light_model / logging / image / keyword / semantic / rerank / review（`data_dir` 为顶层标量，不构成分组）；
- 语义组 `fuse_mode`（pool/concat/weighted_avg）为真实叶子，随普通配置一并合并生效。

## 2. 全量热应用（保存即生效）

管理端 `PUT /admin/config/{group}` 写 DB 后**立即应用，无需重启**：

| 分组类型 | 分组 | 行为 |
|---|---|---|
| 参数类 | thresholds / semantic / review / keyword / image | 直接同步运行配置叶子（阈值字典原子替换、词库/规则热重载） |
| 组件重建类 | embedding / llm / rerank / light_model / cache | **先试建造**（失败 500、DB 不写、旧实例继续生效）→ 落库 → 锁内原子替换；失败自动回滚旧实例并恢复 DB 旧值；rerank 变更会重建 SemanticEngine 以切换重排后端 |
| 纯配置类 | server / logging | 仅同步配置叶子（端口 / 日志绑定类变化**下次启动生效**），响应标注 `apply_scope="config"` |

- 保存响应区分三种语义：`applied=true + runtime` =「已保存并生效」；`applied=true + config` =「已保存并生效（端口/日志类配置于下次启动生效）」；`applied=false` =「已保存（写入配置存储，当前部署未热应用，重启后生效）」；
- embedding / llm 后端切换会重建对应引擎实例：语义引擎按「新 embedding + 现有向量库 + 当前阈值」一并重建；
- 已知业务校验：`backend=cloud` 且 `fuse_mode ∈ weighted_avg/pool`（在线维度 ≠ 本地 CLIP 512）→ 422 建议改用 `concat`。

## 3. 多 Provider 与失败切换（v0.4.0）

Embedding / LLM / Rerank 三类模型均可配置多个提供者：

```yaml
embedding:
  providers:
    - name: local-clip
      backend: local
      local: { model_name: "OFA-Sys/chinese-clip-vit-base-patch16" }
      priority: 1
    - name: cloud-wemm
      backend: cloud
      cloud:
        base_url: "http://127.0.0.1:5545/v1"
        model: "WeMM-Embedding-2B-Q4_K_M.gguf"
        allow_no_key: true
      priority: 2
  active_provider: null       # 手动指定首选；不填按 priority 取最小
  failover:
    enabled: true
    cooldown_seconds: 30
    max_failures: 3

llm:
  providers:
    - name: openai
      base_url: "https://api.openai.com/v1"
      model: "gpt-4o-mini"
      api_key_env: "OPENAI_API_KEY"
      priority: 1
    - name: deepseek
      base_url: "https://api.deepseek.com/v1"
      model: "deepseek-chat"
      api_key_env: "DEEPSEEK_API_KEY"
      priority: 2
  active_provider: null
  failover: { enabled: true, cooldown_seconds: 30, max_failures: 3 }

rerank:
  providers:
    - name: local-clip
      type: local
      priority: 1
    - name: cloud-jina
      type: cloud
      base_url: "https://api.example.com/v1"
      model: "jina-reranker-v2-base-multilingual"
      api_key_env: "JINA_API_KEY"
      priority: 2
  active_provider: null
  failover: { enabled: true, cooldown_seconds: 30, max_failures: 3 }
```

规则：

- `providers` 为空时保持 v0.3.0 单后端兼容行为；
- `active_provider` 手动指定首选，失败仍按 `priority` 切换到其它提供者；
- 每个提供者维护熔断器：连续失败达 `failover.max_failures` 后冷却 `cooldown_seconds` 秒，成功一次即清零；
- `failover.enabled=false` 时不冷却，每次失败立即切下一个；
- Rerank 总开关仍在 `semantic.rerank_enabled`；开启且 `rerank.providers` 为空时回退本地 CLIP 重排。
