# PRD: FileBrowser 跨仓库导航竞态（审计 #8，MED/M）

## 背景

/loop 第 33 轮。承接 07-20-dashboard-resilience-cluster（F73-F75，PR #333 已合），本轮处理当时推迟的 #8。

## 清单（已核实）

### F76: 跨仓库导航时根目录加载竞态 clobber 文件视图（审计 #8，MED/M）

`FileBrowser`（FileBrowser.tsx）`initialNav` effect（外部点击 SearchPanel/TaskDetail/OutputFiles → App.tsx `fileBrowserNav` → `initialNav`）在跨仓库导航时做两件事：
1. `setSelectedRepo(initialNav.repoId)` 切仓库；
2. `loadFile(nav.repoId, nav.path)` 加载目标文件。

但 (1) 会触发 `[selectedRepo]` effect（原 `if (selectedRepo) loadDirectory(selectedRepo, "")`），无条件加载**新仓库的根目录**。于是 `loadFile`（目标文件）与 `loadDirectory`（新仓库根）两个异步并发：

- 若 `getRepoTree`（根目录）后 resolve：`setFileContent(null)`（loadDirectory 入口即清）+ `setEntries(root)` —— 用户点了文件 B，却看到 B 的根目录列表。
- 若 `getRepoFile` 先 resolve：文件闪现后又被 loadDirectory 的 `setFileContent(null)` 清掉再回到根目录。
- `getRepoFile` 慢/失败时：用户卡在 B 的根目录。

同仓库快速连续导航（如连点两个搜索结果）另有 stale-response 竞态：先发的 load 后 resolve 会覆盖后发的视图。

## 修

1. **skip-ref**：新增 `navDrivenRepoChangeRef`。`initialNav` effect 在跨仓库 `setSelectedRepo` 前置该 ref；`[selectedRepo]` effect 检测到 ref 则跳过根目录 autoload 并复位——导航本身已加载目标，根目录 autoload 是多余的且会竞态。
2. **latest-load-wins**：新增 `loadSeqRef`。`loadFile`/`loadDirectory` 共用一个递增 seq；resolve 时 `seq !== loadSeqRef.current` 则丢弃（stale），`finally` 仅 latest 才 `setLoading(false)`。快速连续导航只认最新一次。

dropdown 手动切仓库 / mount 自动选首个仓库：ref 为 false，正常走根目录 autoload，行为不变。

## 验收

- 手动推理 + `tsc -p tsconfig.app.json --noEmit`（所触文件零新错，既有不变）+ `vite build`。
- feature branch + PR + CI green。

## 不做（后续轮）

#11-#15 LOW 杂项收尾轮。
