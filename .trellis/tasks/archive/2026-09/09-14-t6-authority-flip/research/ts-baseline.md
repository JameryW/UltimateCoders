# TS 测试基线（T6 #642 门禁锚点）

- 命令：`cd packages/uc-orchestrator && bun test`
- 基线（2026-09-14，C3 开工前）：**179 pass / 0 fail，17 个测试文件，514 expect() calls**（约 16.4s）
- 要求：T6 全部切片完成后只增不减；C3-C7 删除 wave/review 代码时同步删除其测试不算回归，但净计数不得低于新基线（每刀记录删了哪些测试文件/用例）。
- 环境注意：bun 在 WinGet Links（`bun.exe` 直接可用）；无根 package.json，测试必须进 `packages/uc-orchestrator` 目录跑。
