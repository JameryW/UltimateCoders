/**
 * UltimateCoders Dashboard — SSE client + DOM update logic + interactive operations.
 *
 * Connects to /dashboard/api/stream via EventSource and updates
 * all dashboard panels when new data arrives. Supports POST operations
 * for task pause/resume, circuit breaker reset, scheduler trigger,
 * and pending task flush.
 */

(function () {
    "use strict";

    const STREAM_URL = "/dashboard/api/stream";
    let eventSource = null;
    let connected = false;

    // Mermaid initialization
    if (typeof mermaid !== "undefined") {
        mermaid.initialize({
            startOnLoad: false,
            theme: "dark",
            themeVariables: {
                primaryColor: "#1e293b",
                primaryBorderColor: "#334155",
                primaryTextColor: "#e2e8f0",
                lineColor: "#475569",
                secondaryColor: "#0f172a",
                tertiaryColor: "#1e293b",
            },
            flowchart: { useMaxWidth: true, htmlLabels: true, curve: "basis" },
        });
    }

    // ── Confirm Modal ────────────────────────────────────────────

    let _confirmResolve = null;

    function showConfirm(title, message) {
        return new Promise(function (resolve) {
            _confirmResolve = resolve;
            document.getElementById("confirm-title").textContent = title;
            document.getElementById("confirm-message").textContent = message;
            document.getElementById("confirm-modal").style.display = "flex";
        });
    }

    document.getElementById("confirm-cancel").addEventListener("click", function () {
        document.getElementById("confirm-modal").style.display = "none";
        if (_confirmResolve) { _confirmResolve(false); _confirmResolve = null; }
    });

    document.getElementById("confirm-ok").addEventListener("click", function () {
        document.getElementById("confirm-modal").style.display = "none";
        if (_confirmResolve) { _confirmResolve(true); _confirmResolve = null; }
    });

    // ── Toast Notifications ──────────────────────────────────────

    function showToast(message, type) {
        var container = document.getElementById("toast-container");
        var toast = document.createElement("div");
        toast.className = "toast toast-" + type;
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(function () {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 4000);
    }

    // ── POST Action with Confirm ─────────────────────────────────

    window.confirmAction = function (title, message, url) {
        showConfirm(title, message).then(function (confirmed) {
            if (!confirmed) return;
            fetch(url, { method: "POST" })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data.success) {
                        showToast(title + " succeeded", "success");
                    } else {
                        showToast(title + " failed: " + (data.error || "unknown error"), "error");
                    }
                })
                .catch(function (e) {
                    showToast(title + " failed: " + e.message, "error");
                });
        });
    };

    // ── Connection Management ──────────────────────────────────

    function connect() {
        if (eventSource) {
            eventSource.close();
        }

        eventSource = new EventSource(STREAM_URL);

        eventSource.addEventListener("update", function (event) {
            try {
                var data = JSON.parse(event.data);
                handleUpdate(data);
                setConnected(true);
            } catch (e) {
                console.error("Failed to parse SSE data:", e);
            }
        });

        eventSource.addEventListener("error", function () {
            setConnected(false);
            // EventSource automatically reconnects
        });

        eventSource.addEventListener("open", function () {
            setConnected(true);
        });
    }

    function setConnected(state) {
        connected = state;
        var indicator = document.getElementById("connection-indicator");
        var statusEl = document.getElementById("connection-status");
        if (state) {
            indicator.className = "pulse-dot bg-green-500";
            statusEl.textContent = "Connected";
            statusEl.className = "connection-status bg-green-900 text-green-300";
        } else {
            indicator.className = "pulse-dot bg-red-500";
            statusEl.textContent = "Disconnected";
            statusEl.className = "connection-status bg-red-900 text-red-300";
        }
    }

    // ── Update Handler ─────────────────────────────────────────

    function handleUpdate(data) {
        // Update timestamp
        var lastUpdate = document.getElementById("last-update");
        if (data.timestamp) {
            var d = new Date(data.timestamp);
            lastUpdate.textContent = d.toLocaleTimeString();
        }

        if (data.health) updateHealth(data.health);
        if (data.workers) updateWorkers(data.workers);
        if (data.tasks) updateTasks(data.tasks);
        if (data.scheduler) updateScheduler(data.scheduler);
        if (data.circuit_breaker) updateCircuitBreaker(data.circuit_breaker);
        if (data.events) updateEventLog(data.events);
    }

    // ── Health Panel ───────────────────────────────────────────

    function updateHealth(health) {
        // Overall badge
        var overall = document.getElementById("health-overall");
        if (!health.available) {
            overall.textContent = "Unavailable";
            overall.className = "text-xs px-2 py-0.5 rounded badge-unavailable";
        } else {
            overall.textContent = health.status.toUpperCase();
            overall.className = "text-xs px-2 py-0.5 rounded badge-" + health.status;
        }

        // Components list
        var container = document.getElementById("health-components");
        if (!health.available || !health.components || health.components.length === 0) {
            container.innerHTML = '<p class="text-gray-500">Engine not available</p>';
        } else {
            var html = "";
            for (var i = 0; i < health.components.length; i++) {
                var comp = health.components[i];
                var statusClass = "status-" + comp.status;
                var details = comp.details ? '<span class="text-gray-500 ml-2 text-xs">' + escapeHtml(comp.details) + "</span>" : "";
                html += '<div class="flex items-center justify-between">';
                html += '  <span class="text-gray-300">' + escapeHtml(comp.name) + details + "</span>";
                html += '  <span class="' + statusClass + ' font-medium text-xs uppercase">' + escapeHtml(comp.status) + "</span>";
                html += "</div>";
            }
            container.innerHTML = html;
        }

        // Meta info
        var meta = document.getElementById("health-meta");
        if (health.available) {
            meta.textContent = "Version: " + (health.version || "unknown") + " | Uptime: " + formatUptime(health.uptime_seconds || 0);
        } else {
            meta.textContent = "";
        }
    }

    // ── Workers Panel ──────────────────────────────────────────

    function updateWorkers(workersData) {
        var countEl = document.getElementById("worker-count");
        countEl.textContent = workersData.available ? (workersData.available_count + "/" + workersData.total + " available") : "N/A";

        var container = document.getElementById("workers-list");
        if (!workersData.available || !workersData.workers || workersData.workers.length === 0) {
            container.innerHTML = workersData.available
                ? '<p class="text-gray-500">No workers registered</p>'
                : '<p class="text-gray-500">Not Available</p>';
            return;
        }

        var html = "";
        for (var i = 0; i < workersData.workers.length; i++) {
            var w = workersData.workers[i];
            var loadPercent = w.load_percent;
            var barColor = loadPercent >= 100 ? "bg-red-500" : loadPercent >= 75 ? "bg-yellow-500" : "bg-green-500";
            var staleWarning = w.heartbeat_stale
                ? ' <span class="text-yellow-400 text-xs" title="Heartbeat stale (' + w.heartbeat_age_seconds + 's ago)">&#9888;</span>'
                : "";

            html += '<div class="border-l-2 ' + (w.is_available ? "border-green-500" : "border-red-500") + ' pl-3">';
            html += '  <div class="flex items-center justify-between">';
            html += "    <span>" + escapeHtml(w.id.substring(0, 8)) + staleWarning + "</span>";
            html += '    <span class="text-xs text-gray-400">' + w.current_load + "/" + w.max_capacity + "</span>";
            html += "  </div>";
            html += '  <div class="load-bar mt-1"><div class="load-bar-fill ' + barColor + '" style="width: ' + loadPercent + '%"></div></div>';
            if (w.capabilities && w.capabilities.length > 0) {
                html += '  <div class="text-xs text-gray-500 mt-1">' + escapeHtml(w.capabilities.join(", ")) + "</div>";
            }
            html += "</div>";
        }
        container.innerHTML = html;
    }

    // ── Tasks Panel ────────────────────────────────────────────

    // Store task data for detail expansion
    var _taskDataCache = {};

    function updateTasks(tasksData) {
        var countEl = document.getElementById("task-count");
        countEl.textContent = tasksData.available ? String(tasksData.total) : "N/A";

        // Status counts
        var countsContainer = document.getElementById("task-status-counts");
        if (tasksData.available && tasksData.status_counts) {
            var html = "";
            for (var status in tasksData.status_counts) {
                var count = tasksData.status_counts[status];
                var badgeClass = status === "completed" ? "bg-green-900 text-green-300"
                    : status === "failed" ? "bg-red-900 text-red-300"
                    : status === "paused" ? "bg-yellow-900 text-yellow-300"
                    : status === "in_progress" ? "bg-blue-900 text-blue-300"
                    : "bg-gray-700 text-gray-300";
                html += '<span class="text-xs px-2 py-0.5 rounded ' + badgeClass + '">' + escapeHtml(status) + ": " + count + "</span>";
            }
            countsContainer.innerHTML = html;
        } else {
            countsContainer.innerHTML = "";
        }

        // Pending info + flush button
        var pendingEl = document.getElementById("pending-info");
        var flushBtn = document.getElementById("flush-pending-btn");
        if (tasksData.available && tasksData.pending_task_count > 0) {
            pendingEl.textContent = tasksData.pending_task_count + " task(s) queued (night window)";
            pendingEl.classList.remove("hidden");
            flushBtn.classList.remove("hidden");
        } else {
            pendingEl.classList.add("hidden");
            flushBtn.classList.add("hidden");
        }

        // Task list
        var container = document.getElementById("tasks-list");
        if (!tasksData.available || !tasksData.tasks || tasksData.tasks.length === 0) {
            container.innerHTML = tasksData.available
                ? '<p class="text-gray-500">No tasks</p>'
                : '<p class="text-gray-500">Not Available</p>';
            return;
        }

        // Cache task data for detail expansion
        for (var i = 0; i < tasksData.tasks.length; i++) {
            var t = tasksData.tasks[i];
            _taskDataCache[t.id] = t;
        }

        var html = "";
        for (var j = 0; j < tasksData.tasks.length; j++) {
            var task = tasksData.tasks[j];
            var statusBadge = getStatusBadge(task.status);
            var actionBtn = "";
            if (task.status === "in_progress" || task.status === "planning") {
                actionBtn = ' <button class="btn-action btn-pause" onclick="confirmAction(\'Pause Task\', \'Pause task ' + escapeHtml(task.id.substring(0, 8)) + '?\', \'/dashboard/api/tasks/' + task.id + '/pause\')">Pause</button>';
            } else if (task.status === "paused") {
                actionBtn = ' <button class="btn-action btn-resume" onclick="confirmAction(\'Resume Task\', \'Resume task ' + escapeHtml(task.id.substring(0, 8)) + '?\', \'/dashboard/api/tasks/' + task.id + '/resume\')">Resume</button>';
            }

            html += '<div class="border-l-2 pl-3 ' + getStatusBorderColor(task.status) + ' cursor-pointer" onclick="toggleTaskDetail(\'' + task.id + '\')">';
            html += '  <div class="flex items-center justify-between">';
            html += '    <span class="truncate max-w-[180px]" title="' + escapeHtml(task.description) + '">' + escapeHtml(task.description.substring(0, 40)) + "</span>";
            html += "    " + statusBadge + actionBtn;
            html += "  </div>";
            html += '  <div class="text-xs text-gray-500">' + escapeHtml(task.id.substring(0, 8)) + (task.project_id ? " | " + escapeHtml(task.project_id.substring(0, 15)) : "") + "</div>";
            html += "</div>";
            // Detail expansion area
            html += '<div id="task-detail-' + task.id + '" class="task-detail">';
            html += '  <div id="task-detail-content-' + task.id + '" class="pl-5 py-2 text-xs text-gray-400">Loading...</div>';
            html += "</div>";
        }
        container.innerHTML = html;
    }

    // ── Task Detail Expansion ────────────────────────────────────

    window.toggleTaskDetail = function (taskId) {
        var el = document.getElementById("task-detail-" + taskId);
        if (!el) return;
        if (el.classList.contains("expanded")) {
            el.classList.remove("expanded");
            return;
        }
        el.classList.add("expanded");

        // Fetch full task data
        fetch("/dashboard/api/tasks").then(function (r) { return r.json(); }).then(function (data) {
            if (!data.available || !data.tasks) return;
            var task = null;
            for (var i = 0; i < data.tasks.length; i++) {
                if (data.tasks[i].id === taskId) { task = data.tasks[i]; break; }
            }
            if (!task) return;

            var contentEl = document.getElementById("task-detail-content-" + taskId);
            var html = '<div class="mb-2"><strong class="text-gray-300">Status:</strong> ' + escapeHtml(task.status) + "</div>";

            // Subtask list
            if (task.subtask_count > 0) {
                html += '<div class="mb-2"><strong class="text-gray-300">Subtasks:</strong> ' + task.subtask_count + "</div>";
            }

            // Mermaid DAG — fetch subtask data
            // For now, render a simple subtask summary
            // The full subtask data comes from the task object in SSE
            html += '<div id="mermaid-' + taskId + '" class="mt-2"></div>';

            contentEl.innerHTML = html;

            // Try to render Mermaid DAG if subtask info is available
            var cachedTask = _taskDataCache[taskId];
            if (cachedTask && cachedTask.subtasks && cachedTask.subtasks.length > 0) {
                renderSubtaskDag(taskId, cachedTask.subtasks);
            }
        }).catch(function () {});
    };

    function renderSubtaskDag(taskId, subtasks) {
        if (typeof mermaid === "undefined") return;

        var mermaidEl = document.getElementById("mermaid-" + taskId);
        if (!mermaidEl) return;

        // Build Mermaid graph definition
        var lines = ["graph LR"];
        var idMap = {};
        for (var i = 0; i < subtasks.length; i++) {
            var st = subtasks[i];
            var shortId = "s" + i;
            idMap[st.id] = shortId;
            var statusColor = st.status === "completed" ? "#22c55e"
                : st.status === "failed" ? "#ef4444"
                : st.status === "in_progress" ? "#3b82f6"
                : "#6b7280";
            var label = escapeHtml(st.description.substring(0, 25)).replace(/"/g, "#quot;");
            lines.push("    " + shortId + '["' + label + '"]:::stStyle');
            lines.push("    style " + shortId + " fill:" + statusColor + ",color:#fff,stroke:#334155");
        }
        // Dependency edges
        for (var j = 0; j < subtasks.length; j++) {
            var sub = subtasks[j];
            if (sub.depends_on && sub.depends_on.length > 0) {
                for (var k = 0; k < sub.depends_on.length; k++) {
                    var depId = sub.depends_on[k];
                    if (idMap[depId]) {
                        lines.push("    " + idMap[depId] + " --> " + idMap[sub.id]);
                    }
                }
            }
        }
        lines.push("    classDef stStyle stroke-width:2px,font-size:12px");

        var graphDef = lines.join("\n");
        var renderId = "mermaid-graph-" + taskId.replace(/[^a-zA-Z0-9]/g, "_");

        try {
            mermaid.render(renderId, graphDef).then(function (result) {
                mermaidEl.innerHTML = result.svg;
            }).catch(function () {
                // Mermaid parse error — show text fallback
                mermaidEl.innerHTML = renderSubtaskText(subtasks);
            });
        } catch (e) {
            mermaidEl.innerHTML = renderSubtaskText(subtasks);
        }
    }

    function renderSubtaskText(subtasks) {
        var html = '<div class="space-y-1">';
        for (var i = 0; i < subtasks.length; i++) {
            var st = subtasks[i];
            var statusColor = st.status === "completed" ? "text-green-400"
                : st.status === "failed" ? "text-red-400"
                : st.status === "in_progress" ? "text-blue-400"
                : "text-gray-400";
            html += '<div class="flex items-center space-x-2">';
            html += '  <span class="' + statusColor + '">&#9679;</span>';
            html += '  <span class="text-gray-300">' + escapeHtml(st.description.substring(0, 40)) + "</span>";
            html += '  <span class="text-gray-500 text-xs">(' + escapeHtml(st.status) + ")</span>";
            html += "</div>";
        }
        html += "</div>";
        return html;
    }

    // ── Scheduler Panel ────────────────────────────────────────

    function updateScheduler(schedulerData) {
        var statusEl = document.getElementById("scheduler-status");
        if (!schedulerData.available) {
            statusEl.textContent = "Not Available";
            statusEl.className = "text-xs px-2 py-0.5 rounded badge-unavailable";
            document.getElementById("scheduler-night-window").innerHTML = "";
            document.getElementById("scheduler-jobs").innerHTML = '<p class="text-gray-500">No scheduler configured</p>';
            document.getElementById("scheduler-history").innerHTML = "";
            return;
        }

        // Running status
        statusEl.textContent = schedulerData.is_running ? "RUNNING" : "STOPPED";
        statusEl.className = "text-xs px-2 py-0.5 rounded badge-" + (schedulerData.is_running ? "ok" : "degraded");

        // Night window
        var nightWindowEl = document.getElementById("scheduler-night-window");
        if (schedulerData.night_window) {
            var active = schedulerData.night_window.active;
            nightWindowEl.innerHTML = '<span class="text-gray-400">Night Window:</span> '
                + '<span class="' + (active ? "text-yellow-400" : "text-green-400") + '">'
                + (active ? "ACTIVE" : "INACTIVE") + "</span>";
        } else {
            nightWindowEl.innerHTML = "";
        }

        // Jobs list
        var jobsContainer = document.getElementById("scheduler-jobs");
        if (schedulerData.jobs && schedulerData.jobs.length > 0) {
            var html = "";
            for (var i = 0; i < schedulerData.jobs.length; i++) {
                var job = schedulerData.jobs[i];
                var enabledBadge = job.enabled
                    ? '<span class="text-green-400 text-xs">enabled</span>'
                    : '<span class="text-gray-500 text-xs">disabled</span>';
                var triggerBtn = job.enabled
                    ? ' <button class="btn-action btn-trigger" onclick="confirmAction(\'Trigger Job\', \'Manually trigger ' + escapeHtml(job.description.substring(0, 30)) + '?\', \'/dashboard/api/scheduler/jobs/' + job.id + '/trigger\')">Trigger</button>'
                    : "";
                var schedule = job.cron_expression
                    ? '<span class="text-xs text-gray-400">' + escapeHtml(job.cron_expression) + "</span>"
                    : job.execute_after
                        ? '<span class="text-xs text-gray-400">after ' + escapeHtml(job.execute_after.substring(0, 19)) + "</span>"
                        : "";
                html += '<div class="flex items-center justify-between">';
                html += '  <span class="truncate max-w-[200px]" title="' + escapeHtml(job.description) + '">' + escapeHtml(job.description) + "</span>";
                html += "  " + enabledBadge + triggerBtn;
                html += "</div>";
                html += '<div class="text-xs text-gray-500">' + schedule + "</div>";
            }
            jobsContainer.innerHTML = html;
        } else {
            jobsContainer.innerHTML = '<p class="text-gray-500">No scheduled jobs</p>';
        }

        // Execution history
        var historyContainer = document.getElementById("scheduler-history");
        if (schedulerData.execution_history && schedulerData.execution_history.length > 0) {
            var hhtml = "";
            for (var j = 0; j < schedulerData.execution_history.length; j++) {
                var h = schedulerData.execution_history[j];
                var statusClass = h.status === "Completed" ? "text-green-400"
                    : h.status === "Failed" ? "text-red-400"
                    : h.status === "Deferred" ? "text-yellow-400"
                    : "text-gray-400";
                hhtml += '<div class="flex items-center justify-between">';
                hhtml += '  <span class="text-gray-400">' + escapeHtml(h.task_id.substring(0, 8)) + "</span>";
                hhtml += '  <span class="' + statusClass + '">' + escapeHtml(h.status) + "</span>";
                hhtml += "</div>";
            }
            historyContainer.innerHTML = hhtml;
        } else {
            historyContainer.innerHTML = '<p class="text-gray-500">No execution history</p>';
        }
    }

    // ── Circuit Breaker / Rate Limiter Panel ───────────────────

    function updateCircuitBreaker(cbData) {
        // Circuit Breaker
        var cbContainer = document.getElementById("circuit-breaker-panel");
        var cb = cbData.circuit_breaker;
        var resetBtn = document.getElementById("cb-reset-btn");

        if (!cb || !cb.available) {
            cbContainer.innerHTML = '<p class="text-gray-500">Not Available</p>';
            resetBtn.classList.add("hidden");
        } else {
            var stateClass = "status-" + cb.state.toLowerCase();
            var stateBadge = "badge-" + cb.state.toLowerCase();
            var html = '<div class="flex items-center justify-between">';
            html += '  <span class="text-gray-400">State</span>';
            html += '  <span class="text-xs px-2 py-0.5 rounded ' + stateBadge + '">' + escapeHtml(cb.state) + "</span>";
            html += "</div>";
            html += '<div class="flex items-center justify-between">';
            html += '  <span class="text-gray-400">Failures</span>';
            html += '  <span class="' + (cb.failure_count > 0 ? "text-red-400" : "text-gray-300") + '">' + cb.failure_count + "</span>";
            html += "</div>";
            html += '<div class="flex items-center justify-between">';
            html += '  <span class="text-gray-400">Total Calls</span>';
            html += '  <span class="text-gray-300">' + cb.total_calls + "</span>";
            html += "</div>";
            html += '<div class="flex items-center justify-between">';
            html += '  <span class="text-gray-400">Rejected</span>';
            html += '  <span class="' + (cb.total_rejected > 0 ? "text-yellow-400" : "text-gray-300") + '">' + cb.total_rejected + "</span>";
            html += "</div>";
            // Engine-side circuit breaker details
            if (cbData.engine_circuit_breaker && cbData.engine_circuit_breaker.details) {
                html += '<div class="text-xs text-gray-500 mt-1 pt-1 border-t border-gray-700">' + escapeHtml(cbData.engine_circuit_breaker.details) + "</div>";
            }
            cbContainer.innerHTML = html;
            // Show reset button when CB is open or half_open
            if (cb.state.toLowerCase() === "open" || cb.state.toLowerCase() === "half_open") {
                resetBtn.classList.remove("hidden");
            } else {
                resetBtn.classList.add("hidden");
            }
        }

        // Rate Limiter
        var rlContainer = document.getElementById("rate-limiter-panel");
        var rl = cbData.rate_limiter;
        if (!rl || !rl.available) {
            rlContainer.innerHTML = '<p class="text-gray-500">Not Available</p>';
        } else {
            var rhtml = '<div class="flex items-center justify-between">';
            rhtml += '  <span class="text-gray-400">RPM Available</span>';
            rhtml += '  <span class="text-gray-300">' + rl.rpm_available + "</span>";
            rhtml += "</div>";
            rhtml += '<div class="flex items-center justify-between">';
            rhtml += '  <span class="text-gray-400">TPM Available</span>';
            rhtml += '  <span class="text-gray-300">' + formatNumber(rl.tpm_available) + "</span>";
            rhtml += "</div>";
            rhtml += '<div class="flex items-center justify-between">';
            rhtml += '  <span class="text-gray-400">Active Requests</span>';
            rhtml += '  <span class="text-gray-300">' + rl.active_count + "</span>";
            rhtml += "</div>";
            rhtml += '<div class="flex items-center justify-between">';
            rhtml += '  <span class="text-gray-400">Total Requests</span>';
            rhtml += '  <span class="text-gray-300">' + rl.total_requests + "</span>";
            rhtml += "</div>";
            // Engine-side rate limiter details
            if (cbData.engine_rate_limiter && cbData.engine_rate_limiter.details) {
                rhtml += '<div class="text-xs text-gray-500 mt-1 pt-1 border-t border-gray-700">' + escapeHtml(cbData.engine_rate_limiter.details) + "</div>";
            }
            rlContainer.innerHTML = rhtml;
        }
    }

    // ── Event Log Panel ─────────────────────────────────────────

    function updateEventLog(events) {
        var countEl = document.getElementById("event-count");
        var container = document.getElementById("event-log-list");

        if (!events || events.length === 0) {
            countEl.textContent = "0";
            container.innerHTML = '<p class="text-gray-500">No events</p>';
            return;
        }

        countEl.textContent = String(events.length);
        var html = "";
        for (var i = 0; i < events.length; i++) {
            var ev = events[i];
            var time = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : "--";
            var typeColor = "text-gray-400";
            if (ev.type === "task_pause" || ev.type === "circuit_breaker_reset") typeColor = "text-yellow-400";
            else if (ev.type === "task_resume" || ev.type === "scheduler_trigger") typeColor = "text-green-400";
            else if (ev.type === "flush_pending") typeColor = "text-blue-400";

            var detailParts = [];
            if (ev.details) {
                for (var key in ev.details) {
                    var val = ev.details[key];
                    if (typeof val === "string" && val.length > 12) val = val.substring(0, 12) + "...";
                    detailParts.push(escapeHtml(key) + "=" + escapeHtml(String(val)));
                }
            }
            var detailStr = detailParts.length > 0 ? " <span class='text-gray-500'>" + detailParts.join(", ") + "</span>" : "";

            html += '<div class="event-item">';
            html += '<span class="text-gray-500">' + time + "</span> ";
            html += '<span class="' + typeColor + ' font-medium">' + escapeHtml(ev.type) + "</span>";
            html += detailStr;
            html += "</div>";
        }
        container.innerHTML = html;
    }

    // ── Helpers ────────────────────────────────────────────────

    function escapeHtml(str) {
        if (!str) return "";
        var div = document.createElement("div");
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    function formatUptime(seconds) {
        if (!seconds) return "0s";
        var d = Math.floor(seconds / 86400);
        var h = Math.floor((seconds % 86400) / 3600);
        var m = Math.floor((seconds % 3600) / 60);
        var s = seconds % 60;
        if (d > 0) return d + "d " + h + "h";
        if (h > 0) return h + "h " + m + "m";
        if (m > 0) return m + "m " + s + "s";
        return s + "s";
    }

    function formatNumber(n) {
        if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
        if (n >= 1000) return (n / 1000).toFixed(1) + "K";
        return String(Math.round(n));
    }

    function getStatusBadge(status) {
        var cls = status === "completed" ? "bg-green-900 text-green-300"
            : status === "failed" ? "bg-red-900 text-red-300"
            : status === "paused" ? "bg-yellow-900 text-yellow-300"
            : status === "in_progress" ? "bg-blue-900 text-blue-300"
            : "bg-gray-700 text-gray-300";
        return '<span class="text-xs px-1.5 py-0.5 rounded ' + cls + '">' + escapeHtml(status) + "</span>";
    }

    function getStatusBorderColor(status) {
        if (status === "completed") return "border-green-700";
        if (status === "failed") return "border-red-700";
        if (status === "paused") return "border-yellow-700";
        if (status === "in_progress") return "border-blue-700";
        return "border-gray-600";
    }

    // ── Initialize ─────────────────────────────────────────────

    // Also fetch initial data via REST for faster first paint
    function fetchInitialData() {
        fetch("/dashboard/api/health").then(function (r) { return r.json(); }).then(function (d) { updateHealth(d); }).catch(function () {});
        fetch("/dashboard/api/workers").then(function (r) { return r.json(); }).then(function (d) { updateWorkers(d); }).catch(function () {});
        fetch("/dashboard/api/tasks").then(function (r) { return r.json(); }).then(function (d) { updateTasks(d); }).catch(function () {});
        fetch("/dashboard/api/scheduler").then(function (r) { return r.json(); }).then(function (d) { updateScheduler(d); }).catch(function () {});
        fetch("/dashboard/api/circuit-breaker").then(function (r) { return r.json(); }).then(function (d) { updateCircuitBreaker(d); }).catch(function () {});
        fetch("/dashboard/api/events").then(function (r) { return r.json(); }).then(function (d) { updateEventLog(d.events); }).catch(function () {});
    }

    fetchInitialData();
    connect();
})();
