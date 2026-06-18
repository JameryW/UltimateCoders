# Dashboard: Auth gate, Dark/Light toggle, URL routing

## Goal

完成 Dashboard 的最后三个功能缺口：基本认证保护、主题切换、URL 路由。

## Requirements

1. **Auth gate**: 非 localhost 访问时要求基本认证（可配置密码）；localhost 无需认证
2. **Dark/Light toggle**: Header 中添加主题切换按钮，使用 CSS 变量实现
3. **URL routing**: 各面板有独立 URL，支持书签和导航

## Acceptance Criteria

- [ ] 非 localhost 访问 Dashboard 需要输入密码（环境变量 `DASHBOARD_PASSWORD` 配置）
- [ ] Header 有主题切换按钮，dark/light 两种模式
- [ ] 各面板有 URL hash 路由（#tasks, #events, #workers 等）

## Out of Scope

* JWT/OAuth（后续部署需要时再加）
* 角色权限（admin/viewer）
* 任务提交历史
