# PRD: Dashboard gRPC Auth Interceptor

## 背景

dashboard agent 审计 finding #4。Rust gRPC server 无 auth interceptor；dashboard `useAuth.validateToken` 用 `application/json` 裸 fetch 打 gRPC-Web Health 端点，非 401/403 返 true → 任意 password 可过（前端-only gate，非真安全边界）。

用户选「Rust interceptor」方案：加 tonic interceptor 做真后端鉴权。

## 设计

### Rust 侧（crates/uc-grpc + uc-grpc-server）

1. **token 配置**：env `UC_DASHBOARD_TOKEN`。空 = 无 auth gate（向后兼容 dev/local，不破坏现有部署）；非空 = 保护所有业务 service。
2. **interceptor**：`tonic::service::interceptor::Interceptor` 校验 `Authorization: Bearer <token>`。匹配 → 放行；不匹配/缺失（当 token 配置时）→ `Status::unauthenticated`。
3. **保护范围**：EngineService / TaskService / DashboardService / WorkerService 全保护。**tonic_health 标准健康服务不保护**（kube/docker probe 兼容）。
4. **main.rs**：`UC_DASHBOARD_TOKEN` 非空时，5 个业务 service 用 `InterceptedService::new(svc, auth_interceptor)` 包；health_service 不包。

### TS 侧（dashboard/src/hooks/useAuth.ts）

5. **validateToken 改用 Connect client**（非裸 fetch application/json）：用 `@connectrpc/connect` createClient 打 EngineService.Health，header 带 `Authorization: Bearer <token>`。200 = 有效；Unauthenticated = 无效。
6. **mount 无 token 时**：若 server 配置了 token → Health 401 → isAuthenticated=false（显示登录）；若 server 无 token → Health 200 → isAuthenticated=true（无 gate，兼容）。
7. **login**：password 作 Bearer token 验，成功存 localStorage。

## 验收

- cargo check + cargo test -p uc-grpc。
- interceptor 单测：token 配置时，无/错 Bearer → unauthenticated；正确 Bearer → 放行；无 token 配置 → 全放行。
- vite build + tsc App/useAuth clean。
- feature branch + PR。
- PR 后查 CI（ci-rust + ci-dashboard）。

## 不做

- 不改 tonic_health 标准健康服务（probe 兼容）。
- 不加 token 轮换/多用户（单 shared token，YAGNI）。
- 不改 dashboard SSE auth（SSE 端点在 Python dashboard，另立）。
