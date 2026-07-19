# PRD: TUI Cleanup Sweep — Round 7 审计 LOW 项收尾

## 背景

/loop 第 11 轮。Round 7 审计清单仅剩 LOW 项：#12（error 分类误报）、#13（progress-widget 三处打磨）、#9-deps（status-formatter deps 后缀预算）。本 PR 收掉，审计清单清零。

## 改

### F23: error 分类词边界（审计 #12）

裸码 "503"/"429"/"529"/"400"/"401"/"403"/"404" 走 `includes()` 子串匹配 → 误报："processed 400 items"、"port 8429"、路径 "404.html"。

修：裸码拆出为 regex `(?<![.\d])(503|429|529)(?![.\d])` / `(?<![.\d])(400|401|403|404)(?![.\d])`（lookbehind/ahead 拒相邻数字与点——"8429" 前导数字、"404.html" 后导点皆不匹配）；友好字符串 marker 保持 includes()。transient label 色 error→warning（示"可重试"，permanent 保持 error 红）。

### F24: progress-widget 三处打磨（审计 #13）

1. **Wave X/Y 标签误导**：进度条按**全任务** completed/total 算，标签却写 Wave X/Y（像本 wave 进度）。修：行重组——`${bar} ${completed}/${total} · wave ${waveIdx+1}/${totalWaves}`，进度主体在前、wave 上下文后缀。
2. **lastRender 死字段**：3 处写 0 处读（selfcheck 亦无引用）。删字段 + 赋值 + invalidate 体（方法留空壳，Component 接口要求）。
3. **running 行 desc 预算 width-12 假设短 id**：planner 接受 LLM 自选 id（可长）。修：`descBudget = width - (6 + st.id.length)`（"  " + icon 可视 1 + " " + id + ": "）。

### F25: status-formatter deps 后缀预算（审计 #9 遗留）

deps `←id1,id2` 在 capped desc 后追加，无 width 预算 → 多/长 dep id 溢出行。

修：depsPlain 长度计入 descBudget 扣减；depsPlain 自身超 width/2 时折叠为 `←+N deps`（保行不溢）。

## 验收

- error-format.test.ts：误报用例（"processed 400 items"/"port 8429"/"404.html"）→ unknown；真码（"HTTP 503"/"Error: 401 unauthorized"）→ 对应 kind；transient label warning 色（selfcheck theme 验 fg 调用色参数）。
- progress-widget.selfcheck.ts：wave 行含 "· wave"；running 行长 id desc 行 ≤ width；lastRender 删除无回归（既有断言全绿）。
- status-formatter.selfcheck.ts：多 deps 行整行 ≤ width；deps 折叠 `+N deps`。
- bun test test src + tsc（src/ 零错误）+ feature branch + PR + CI green。

## 不做

- /uc search path 复制（notify toast 接线，另评估）。
- 新一轮审计 / Rust/Python 审计（本清单清零后转向下轮决策）。
