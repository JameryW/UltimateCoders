"""Generate bilingual architecture SVGs for the README and reference guide.

Run from any directory: python scripts/generate-architecture.py
Use --check to verify that the checked-in illustrations match this source.
Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
LABELS = {
    "title": ("System architecture", "系统架构"),
    "subtitle": (
        "Python plans · Rust dispatches · Workers execute",
        "Python 规划 · Rust 调度 · Worker 执行",
    ),
    "description": (
        "Service overview of UltimateCoders. Dashboard and optional OMP "
        "connect to the Rust Gateway; "
        "the Dashboard API and Python planner exchange messages through NATS. "
        "The Gateway dispatches "
        "ready nodes to Python Workers through JetStream; Workers register through gRPC. "
        "Shared services provide search, memory, persistence and model APIs. Optional MetaInfer "
        "produces inference candidates; UC benchmarks and accepts or rolls them back.",
        "UltimateCoders 服务架构：Dashboard 和可选 OMP 连接 Rust Gateway；Dashboard API、Python "
        "规划器通过 NATS 交换消息。Gateway 经 JetStream 派发就绪节点，Worker 经 gRPC 注册。"
        "共享服务提供检索、记忆、持久化和模型 API。可选 MetaInfer 生成推理候选，"
        "UC 负责基准测试、验收和回滚。",
    ),
    "messages": ("Runtime messages", "运行时消息"),
    "optional": ("Optional", "可选"),
    "interaction": ("INTERACTION", "交互入口"),
    "control": ("PLANNING & CONTROL", "规划与控制"),
    "execution": ("DISTRIBUTED EXECUTION", "分布式执行"),
    "dashboard": ("Web Dashboard", "Web Dashboard"),
    "dashboard_detail": ("React · gRPC-Web", "React · gRPC-Web"),
    "dashboard_api": ("HTTP / SSE via API", "经 API 使用 HTTP / SSE"),
    "api": ("Dashboard API", "Dashboard API"),
    "api_detail": ("FastAPI · REST / SSE", "FastAPI · REST / SSE"),
    "omp": ("OMP terminal", "OMP 终端"),
    "omp_detail": ("Optional · gRPC", "可选 · gRPC"),
    "gateway": ("Rust Gateway", "Rust Gateway"),
    "gateway_state": ("Task state + ready dispatch", "任务状态与就绪节点派发"),
    "gateway_workers": ("WorkerRegistry + controls", "Worker 注册与任务控制"),
    "gateway_recovery": ("WatchTask + recovery", "WatchTask 与恢复"),
    "planner": ("Python Planner", "Python 规划器"),
    "planner_dag": ("DAG + domain routing", "DAG 规划与领域路由"),
    "planner_config": ("Execution config snapshots", "完整执行配置快照"),
    "planner_model": ("Planning model selection", "规划模型选择"),
    "nats": ("NATS", "NATS"),
    "nats_detail": (
        "Core: submit / events · JetStream: execute / history",
        "Core：提交 / 事件 · JetStream：执行 / 历史",
    ),
    "workers": ("Python Workers", "Python Worker"),
    "worker_worktree": ("Isolated Git worktrees", "隔离 Git worktree"),
    "worker_domains": ("Coding adapters", "编程适配器"),
    "worker_adapters": ("InferenceInfraAgent", "InferenceInfraAgent"),
    "consumers": ("NATS consumers", "NATS 消费者"),
    "consumer_detail": ("Execute + report", "执行与结果上报"),
    "grpc_workers": ("gRPC: register / heartbeat", "gRPC：注册 / 心跳"),
    "dispatch_results": ("dispatch / results", "派发 / 结果"),
    "submit_dag": ("submit / DAG", "提交 / DAG"),
    "core_results": ("Core results", "Core 结果"),
    "execute": ("execution", "执行"),
    "feedback": (
        "Live updates: WatchTask / SSE → Dashboard · "
        "Recovery: ordered EventStore + checkpoint / replay",
        "实时更新：WatchTask / SSE → Dashboard · 恢复：有序 EventStore + checkpoint / replay",
    ),
    "shared": ("SHARED SERVICES", "共享服务"),
    "knowledge": ("Knowledge & persistence", "知识与持久化"),
    "knowledge_search": ("Text / Semantic / AST", "文本 / 语义 / AST 检索"),
    "knowledge_storage": ("TiKV · Qdrant · PostgreSQL", "TiKV · Qdrant · PostgreSQL"),
    "knowledge_scope": ("Gateway search, memory + metadata", "Gateway 检索、记忆与元数据"),
    "models": ("Model providers", "模型服务"),
    "model_backends": ("Cloud APIs / local Ollama", "云端 API / 本地 Ollama"),
    "model_scope": ("Planner + compatible coding adapters", "规划器与兼容的编程适配器"),
    "model_config": ("Independent provider configuration", "规划和编程模型分别配置"),
    "metainfer": ("External MetaInfer", "外部 MetaInfer"),
    "metainfer_tools": ("Specialized generation + GPU tools", "专业生成与 GPU 工具"),
    "metainfer_workspace": ("HTTP + shared assigned worktree", "HTTP + 共享已分配 worktree"),
    "metainfer_scope": ("Invoked by the inference workflow", "由推理工作流调用"),
    "acceptance": (
        "OPTIONAL INFERENCE ACCEPTANCE · UC OWNS THE VERDICT",
        "可选推理验收流程 · UC 负责最终验收",
    ),
    "baseline": ("Baseline", "固定基线"),
    "baseline_detail": ("Fixed UC workload", "UC 保护的固定工作负载"),
    "candidate": ("Candidate", "生成候选"),
    "candidate_detail": ("External MetaInfer", "外部 MetaInfer"),
    "measure": ("Benchmark + Oracle", "Benchmark + Oracle"),
    "measure_detail": ("Compile · benchmark · profile", "编译 · 基准测试 · 性能分析"),
    "verdict": ("Accept / rollback", "接受 / 回滚"),
    "verdict_detail": ("UC verdict + saved artifacts", "UC 验收与持久化产物"),
    "evidence": (
        "Accepted evidence → project Memory · adaptation graph records experiment provenance",
        "已验收证据 → 项目 Memory · 执行适配图记录实验来源",
    ),
    "activation": (
        "Default: TaskStore + durable events · "
        "Graph attempts: UC_DATABASE_URL + UC_GRAPH_SHADOW=on.",
        "默认：TaskStore + 持久事件 · Graph Attempt：UC_DATABASE_URL + UC_GRAPH_SHADOW=on。",
    ),
    "overview_description": (
        "Overview of UltimateCoders: Dashboard and optional OMP submit tasks; "
        "Python plans DAGs and Rust owns task state and dispatch. NATS connects the control "
        "plane to scalable Workers in isolated Git worktrees. Shared services supply search, "
        "memory and models. Optional MetaInfer supplies specialized GPU tools, with UC acceptance.",
        "UltimateCoders 概览：Dashboard 和可选 OMP 提交任务；Python 规划 DAG，"
        "Rust 管理任务状态与调度。NATS 连接控制层和隔离 worktree 中的可扩展 Worker。"
        "共享服务提供检索、记忆和模型；可选 MetaInfer 提供 GPU 工具，UC 负责验收。",
    ),
    "overview_entry": ("Entry points", "交互入口"),
    "overview_control": ("Planning & control", "规划与控制"),
    "overview_workers": ("Worker pool", "Worker 集群"),
    "overview_gateway": ("State · dispatch · recovery", "状态 · 调度 · 恢复"),
    "overview_planner": ("DAG · domain routing", "DAG · 领域路由"),
    "overview_isolation": ("Sandbox + Git worktrees", "Sandbox + Git worktree"),
    "overview_adapters": ("Coding / inference adapters", "编程 / 推理适配器"),
    "overview_scale": ("Scale from 1 to N workers", "从 1 个扩展到 N 个 Worker"),
    "overview_omp": ("OMP terminal · optional", "OMP 终端 · 可选"),
    "overview_transport": (
        "NATS · Core messages / JetStream delivery",
        "NATS · Core 消息 / JetStream 投递",
    ),
    "overview_memory": ("Search + Memory", "检索与 Memory"),
    "overview_knowledge": ("Text · Semantic · AST", "文本 · 语义 · AST"),
    "overview_metainfer": ("GPU tools · UC acceptance", "GPU 工具 · UC 验收"),
    "overview_status": (
        "WatchTask / SSE · durable events · checkpoint / replay",
        "WatchTask / SSE · 持久事件 · checkpoint / replay",
    ),
}


class Diagram:
    """Small SVG primitives shared by every view and language."""

    def __init__(self, language: str, width: int, height: int, description: str) -> None:
        self.locale = 0 if language == "en" else 1
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" '
            f'xml:lang="{language}" role="img" aria-labelledby="title desc">',
            f'  <title id="title">UltimateCoders · {escape(LABELS["title"][self.locale])}</title>',
            f'  <desc id="desc">{escape(LABELS[description][self.locale])}</desc>',
            "  <defs>",
            '    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="10" markerHeight="10" markerUnits="userSpaceOnUse" '
            'orient="auto-start-reverse">',
            '      <path d="M1 1 L9 5 L1 9 Z" fill="#60758c"/>',
            "    </marker>",
            "    <style>",
            '      text { font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", '
            '"Noto Sans CJK SC", Arial, sans-serif; fill: #334155; }',
            "      .heading { font-weight: 600; fill: #0f172a; }",
            "      .section { font-weight: 600; letter-spacing: 1.5px; }",
            "      .muted { fill: #64748b; }",
            "      .connector { fill: none; stroke: #60758c; stroke-width: 2; "
            "stroke-linejoin: round; }",
            "    </style>",
            "  </defs>",
            f'  <rect width="{width}" height="{height}" fill="#ffffff"/>',
        ]

    def text(
        self, key: str, x: int, y: int, size: int = 19, css: str = "", anchor: str = "start"
    ) -> None:
        value = LABELS[key][self.locale] if key in LABELS else key
        self.parts.append(
            f'  <text x="{x}" y="{y}" font-size="{size}" class="{css}" '
            f'text-anchor="{anchor}">{escape(value)}</text>'
        )

    def rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        fill: str,
        stroke: str = "none",
        optional: bool = False,
    ) -> None:
        dash = ' stroke-dasharray="6 5"' if optional else ""
        self.parts.append(
            f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" '
            f'fill="{fill}" stroke="{stroke}"{dash}/>'
        )

    def edge(self, path: str, both: bool = False) -> None:
        start = ' marker-start="url(#arrow)"' if both else ""
        self.parts.append(f'  <path d="{path}" class="connector" marker-end="url(#arrow)"{start}/>')

    def card(
        self,
        key: str,
        x: int,
        y: int,
        w: int,
        h: int,
        lines: list[str],
        title_size: int = 25,
        optional: bool = False,
    ) -> None:
        self.parts.append(f'  <g data-card="{key}">')
        self.rect(x, y, w, h, "#ffffff", "#cbd5e1", optional)
        self.text(key, x + 20, y + 37, title_size, "heading")
        for index, line in enumerate(lines):
            self.text(line, x + 20, y + 73 + index * 28, 19)
        self.parts.append("  </g>")

    def finish(self) -> str:
        return "\n".join([*self.parts, "</svg>"]) + "\n"


def render(language: str) -> str:
    """Detailed service view with protocol routes and acceptance workflow."""
    diagram = Diagram(language, 1440, 1140, "description")
    text, rect, edge, card = diagram.text, diagram.rect, diagram.edge, diagram.card

    # Header and legend. No promotional headline competes with the diagram.
    text("ULTIMATECODERS", 40, 48, 16, "section muted")
    text("title", 40, 98, 38, "heading")
    text("subtitle", 40, 136, 21, "muted")
    edge("M1080 82 H1112")
    text("messages", 1126, 88, 18, "muted")
    rect(1080, 109, 32, 22, "#ffffff", "#94a3b8", True)
    text("optional", 1126, 127, 18, "muted")

    # Three service boundaries. NATS is the real message hub, not a stage
    # after an invented direct client-to-planner call.
    rect(40, 202, 264, 408, "#f1f5fb", "#dbe5f1")
    rect(370, 202, 682, 408, "#f0f8f6", "#d8e9e4")
    rect(1116, 202, 284, 408, "#f6f3fb", "#e3dcf0")
    text("interaction", 60, 234, 16, "section")
    text("control", 396, 234, 16, "section")
    text("execution", 1136, 234, 15, "section")

    # Worker handshake has its own route above the group boundaries.
    edge("M640 262 V174 H1090 V284 H1136", both=True)
    rect(766, 152, 249, 29, "#ffffff")
    text("grpc_workers", 890, 173, 17, "muted", "middle")

    card("dashboard", 60, 248, 224, 122, ["dashboard_detail", "dashboard_api"], 23)
    card("api", 60, 492, 224, 94, ["api_detail"], 23)
    rect(60, 400, 224, 74, "#ffffff", "#94a3b8", True)
    text("omp", 80, 430, 21, "heading")
    text("omp_detail", 80, 458, 18, "muted")
    card("gateway", 396, 262, 292, 160, ["gateway_state", "gateway_workers", "gateway_recovery"])
    card("planner", 734, 262, 292, 160, ["planner_dag", "planner_config", "planner_model"])
    card("nats", 396, 492, 630, 94, ["nats_detail"])
    card(
        "workers", 1136, 262, 244, 160, ["worker_worktree", "worker_domains", "worker_adapters"], 23
    )
    card("consumers", 1136, 492, 244, 94, ["consumer_detail"], 22)

    edge("M284 308 H396", both=True)
    text("gRPC-Web", 340, 295, 16, "muted", "middle")
    edge("M284 539 H396", both=True)
    text("NATS", 340, 526, 16, "muted", "middle")
    edge("M284 437 H354 V385 H396", both=True)
    edge("M542 422 V492", both=True)
    text("dispatch_results", 554, 461, 16, "muted")
    edge("M880 422 V492", both=True)
    text("submit_dag", 892, 461, 16, "muted")
    edge("M1026 527 H1136")
    text("JetStream", 1081, 514, 16, "muted", "middle")
    edge("M1136 562 H1026")
    text("core_results", 1081, 587, 16, "muted", "middle")
    edge("M1258 422 V492", both=True)
    text("execute", 1270, 461, 16, "muted")

    # Summaries keep cross-cutting concerns visible without long wires.
    rect(40, 630, 1360, 42, "#f8fafc")
    text("feedback", 60, 657, 18, "muted")
    text("shared", 40, 709, 16, "section muted")
    card(
        "knowledge",
        40,
        728,
        436,
        160,
        ["knowledge_search", "knowledge_storage", "knowledge_scope"],
        25,
    )
    card("models", 498, 728, 432, 160, ["model_backends", "model_scope", "model_config"], 25)
    card(
        "metainfer",
        952,
        728,
        448,
        160,
        ["metainfer_tools", "metainfer_workspace", "metainfer_scope"],
        25,
        True,
    )
    rect(1300, 743, 78, 25, "#f8fafc")
    text("optional", 1339, 761, 15, "muted", "middle")

    # UC acceptance is a separate optional workflow. MetaInfer produces a
    # candidate; UC measures it, decides, and persists evidence.
    rect(40, 926, 1360, 150, "#fcfaf5", "#d6c8a9", True)
    text("acceptance", 64, 954, 16, "section")
    for x, key, detail in [
        (64, "baseline", "baseline_detail"),
        (404, "candidate", "candidate_detail"),
        (744, "measure", "measure_detail"),
        (1112, "verdict", "verdict_detail"),
    ]:
        text(key, x, 993, 24, "heading")
        text(detail, x, 1022, 18, "muted")
    edge("M310 987 H382")
    edge("M650 987 H722")
    edge("M1018 987 H1090")
    text("evidence", 64, 1055, 18, "muted")
    text("activation", 40, 1114, 18, "muted")
    return diagram.finish()


def render_overview(language: str, *, portrait: bool = False) -> str:
    """Show responsibilities without duplicating the detailed protocol map."""
    width, height = (620, 1436) if portrait else (1200, 800)
    diagram = Diagram(language, width, height, "overview_description")
    text, rect, edge = diagram.text, diagram.rect, diagram.edge
    text("ULTIMATECODERS", 32, 44, 16, "section muted")
    text("title", 32, 94, 36, "heading")
    text("subtitle", 32, 132, 21, "muted")

    # Arrows describe the task lifecycle at this level, not wire protocols.
    # OMP remains visibly optional beside the two default Web components.
    if portrait:
        panels = [(32, 168, 556, 264), (32, 470, 556, 230), (32, 738, 556, 218)]
        edge("M310 432 V470")
        edge("M310 700 V738")
        transport_y, services_y = 978, 1088
    else:
        panels = [(32, 180, 336, 300), (432, 180, 336, 300), (832, 180, 336, 300)]
        edge("M368 330 H432")
        edge("M768 330 H832")
        transport_y, services_y = 502, 664

    for (x, y, w, h), key, color, stroke in zip(
        panels,
        ["overview_entry", "overview_control", "overview_workers"],
        ["#f1f5fb", "#f0f8f6", "#f6f3fb"],
        ["#dbe5f1", "#d8e9e4", "#e3dcf0"],
    ):
        rect(x, y, w, h, color, stroke)
        text(key, x + 24, y + 40, 25, "heading")

    x, y, w, _ = panels[0]
    text("dashboard", x + 24, y + 90, 24, "heading")
    text("dashboard_detail", x + 24, y + 122, 21, "muted")
    text("api", x + 24, y + 166, 24, "heading")
    text("api_detail", x + 24, y + 198, 21, "muted")
    rect(x + 24, y + 220, w - 48, 40, "#ffffff", "#94a3b8", True)
    text("overview_omp", x + 40, y + 247, 21)

    x, y, _, _ = panels[1]
    text("planner", x + 24, y + 90, 24, "heading")
    text("overview_planner", x + 24, y + 122, 21, "muted")
    text("gateway", x + 24, y + 174, 24, "heading")
    text("overview_gateway", x + 24, y + 206, 21, "muted")

    x, y, _, _ = panels[2]
    text("workers", x + 24, y + 90, 24, "heading")
    text("overview_scale", x + 24, y + 122, 21, "muted")
    text("overview_isolation", x + 24, y + 174, 21)
    text("overview_adapters", x + 24, y + 206, 21)

    # The message hub and observability span the runtime. Shared-service
    # cards intentionally have no crossing dependency lines in this overview.
    rect(32, transport_y, width - 64, 52, "#f8fafc", "#e2e8f0")
    text("overview_transport", width // 2, transport_y + 33, 23, "heading", "middle")
    if portrait:
        text("shared", 32, services_y - 20, 16, "section muted")
        service_cards = [
            (32, services_y, 556),
            (32, services_y + 100, 556),
            (32, services_y + 200, 556),
        ]
    else:
        rect(32, 572, width - 64, 44, "#f8fafc")
        text("overview_status", width // 2, 601, 21, "muted", "middle")
        text("shared", 32, services_y - 20, 16, "section muted")
        service_cards = [(32, services_y, 368), (416, services_y, 368), (800, services_y, 368)]
    for (x, y, w), key, line in zip(
        service_cards,
        ["overview_memory", "models", "metainfer"],
        ["overview_knowledge", "model_backends", "overview_metainfer"],
    ):
        rect(x, y, w, 88 if portrait else 104, "#ffffff", "#cbd5e1", key == "metainfer")
        text(key, x + 24, y + 36, 24, "heading")
        text(line, x + 24, y + 69, 21, "muted")
        if key == "metainfer":
            text("optional", x + w - 24, y + 35, 17, "muted", "end")
    if portrait:
        text("WatchTask / SSE · checkpoint / replay", 32, 1410, 22, "muted")
    return diagram.finish()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check outputs without modifying them")
    args = parser.parse_args()
    for language, suffix in [("en", ""), ("zh-CN", ".zh-CN")]:
        for name, svg in [
            ("system-architecture", render(language)),
            ("architecture-overview", render_overview(language)),
            ("architecture-overview-mobile", render_overview(language, portrait=True)),
        ]:
            destination = OUTPUT / f"{name}{suffix}.svg"
            if args.check:
                if not destination.exists() or destination.read_text(encoding="utf-8") != svg:
                    print(f"Out of date: {destination.name}")
                    return 1
                print(f"Up to date: {destination.name}")
            else:
                destination.write_bytes(svg.encode("utf-8"))
                print(f"Generated: {destination.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
