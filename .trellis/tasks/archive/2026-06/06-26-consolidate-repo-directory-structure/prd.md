# Consolidate Repo Directory Structure (Round 2)

## Goal

进一步精简合并 UltimateCoders 仓库目录，消除冗余文件和矛盾配置，让结构更清晰。

## Requirements

1. **合并 run_tui.sh → run-omp.sh，删 scripts/**：将 `--server` 和 `--build` flag 加入 run-omp.sh，删除 scripts/run_tui.sh 和整个 scripts/ 目录
2. **移除 .so git 跟踪**：`git rm --cached python/ultimate_coders/_uc_core.cpython-314-darwin.so`
3. **删除 buf.yaml / buf.gen.yaml**：未被使用的 buf 配置
4. **docker-compose*.yml 移入 docker/**：统一 Docker 文件位置
5. **简化 tests/**：删除顶层 `tests/__init__.py`

## Acceptance Criteria

* [ ] scripts/ 目录已删除
* [ ] run-omp.sh 支持 `--server` 和 `--build` flag
* [ ] .so 文件不再被 git 跟踪
* [ ] buf.yaml / buf.gen.yaml 已删除
* [ ] docker-compose*.yml 在 docker/ 目录下
* [ ] tests/__init__.py 已删除
* [ ] README.md / CLAUDE.md 引用已更新
* [ ] `cargo check` 通过
* [ ] 无 git 跟踪的构建产物

## Definition of Done

* CI green
* 文档与实际结构一致
* 无冗余文件

## Technical Approach

* run-omp.sh: 加入 `--server` (gRPC server) + `--build` (maturin develop) flags，来自 run_tui.sh 的逻辑
* .so: `git rm --cached` 即可，.gitignore 已有规则
* buf: 直接删除，engine_pb.ts 已存在不受影响
* docker-compose: `git mv` 到 docker/，更新文档引用
* tests/__init__.py: 直接删除

## Out of Scope

* 代码逻辑重构
* vendor/oh-my-pi 子模块变更
* .claude/ .trellis/ 内部结构
