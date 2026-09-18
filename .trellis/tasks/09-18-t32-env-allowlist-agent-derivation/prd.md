# T32 —— sandbox env allowlist 测试的 agent 清单改为从 allowlist 自身推导

承接 **#682**（#644 残余 2 的落地票）。属**框架卫生线**（与 T21–T31 同类），**不依赖**外部「方案第 21 节」。

## 1. 起点与判据

`tests/python/test_sandbox_env_allowlist.py:51` 的 `ALL_AGENTS` 是**手抄清单**，与
`python/ultimate_coders/agent/sandbox.py:80` 的 `ADAPTER_ENV_ALLOWLIST` **平行维护**。

2026-09-18 实测两侧的内容与数量：

| 侧 | 内容 | 条数 |
|---|---|---|
| `ADAPTER_ENV_ALLOWLIST` 的键 | grok-build / claude-code / claude-code-decompose / codex / deepseek-harness / local-harness | **6** |
| 测试的 `ALL_AGENTS` | 上面 6 个 + `grok`（别名）+ `some-external-plugin`（刻意未知） | **8** |

⇒ **失效模式**：新增 adapter（= allowlist 加一个键）时，测试**不自动纳入**参数化，
新适配器的「host secret 不得进入子进程」断言**静默缺失**，而测试**全绿**。
与 T13 教训同源：**同一规则留了两份实现，必然分叉**。

## 2. 修法裁决

**从 allowlist 自身推导**，而不是再抄一份：

```
ALL_AGENTS = [*ADAPTER_ENV_ALLOWLIST,
              *[a for a in GROK_AGENT_ALIASES if a not in ADAPTER_ENV_ALLOWLIST],
              UNKNOWN_AGENT]
```

- 别名那一支**要保留**：它覆盖「别名不直接命中 allowlist、须经 registry 归一后才命中」的路径（`:247` `child_env_allowlist` 的 `canonical` 归一）。
- `UNKNOWN_AGENT = "some-external-plugin"` 保留为**刻意未知项**（`:170`/`:183` 用它证明「未知 ⇒ 只有 base+shared」，即 deny-by-default 而非「已知才挡」）。

### 非目标（都写进票面，避免下一个人扩大战线）

- **不**改 `ADAPTER_ENV_ALLOWLIST` 的内容 —— 本票只修**测试侧的漂移面**。
- **不**改用 `available_agents()`：它内部会跑 `discover_once()`（`registry.py:374-378`），
  会让参数化集合**依赖宿主环境**（装了外部 entry point 的机器条数就变），
  从而破坏「总收集数对账」这条回归判据。**判据必须与宿主无关。**

## 3. 交付

1. `ALL_AGENTS` / `SPAWNABLE_AGENTS` 改为推导式；`some-external-plugin` 提为 `UNKNOWN_AGENT` 常量，
   消掉 `:64` / `:170` / `:183` 三处同名字面量。
2. 新增**元测试**：断言 `ALL_AGENTS` 覆盖 `ADAPTER_ENV_ALLOWLIST` 的**每一个键** ——
   若有人把它改回手抄清单，这条会红。

## 4. 验收

| # | 判据 | 期望 |
|---|---|---|
| 1 | 参数化条目数 | **8 ⇒ 8** 不变 |
| 2 | 消融：给 allowlist 临时加一个键 | `ALL_AGENTS` **自动多一条**（测试文件不动），移除后恢复 |
| 3 | `pytest tests/python/test_sandbox_env_allowlist.py` | 全绿 |
| 4 | Python 总收集数 | **1210 ⇒ 1211**（只 +1 = 新增元测试） |
