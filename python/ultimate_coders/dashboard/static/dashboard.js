/**
 * UltimateCoders Dashboard — SSE client + DOM update logic.
 *
 * Connects to /dashboard/api/stream via EventSource and updates
 * all dashboard panels when new data arrives. EventSource handles
 * automatic reconnection on disconnect.
 */

(function () {
    "use strict";

    const STREAM_URL = "/dashboard/api/stream";
    let eventSource = null;
    let connected = false;

    // ── Connection Management ──────────────────────────────────

    function connect() {
        if (eventSource) {
            eventSource.close();
        }

        eventSource = new EventSource(STREAM_URL);

        eventSource.addEventListener("update", function (event) {
            try {
                const data = JSON.parse(event.data);
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
        const indicator = document.getElementById("connection-indicator");
        const statusEl = document.getElementById("connection-status");
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
        const lastUpdate = document.getElementById("last-update");
        if (data.timestamp) {
            const d = new Date(data.timestamp);
            lastUpdate.textContent = d.toLocaleTimeString();
        }

        if (data.health) updateHealth(data.health);
        if (data.workers) updateWorkers(data.workers);
        if (data.tasks) updateTasks(data.tasks);
        if (data.scheduler) updateScheduler(data.scheduler);
        if (data.circuit_breaker) updateCircuitBreaker(data.circuit_breaker);
    }

    // ── Health Panel ───────────────────────────────────────────

    function updateHealth(health) {
        // Overall badge
        const overall = document.getElementById("health-overall");
        if (!health.available) {
            overall.textContent = "Unavailable";
            overall.className = "text-xs px-2 py-0.5 rounded badge-unavailable";
        } else {
            overall.textContent = health.status.toUpperCase();
            overall.className = "text-xs px-2 py-0.5 rounded badge-" + health.status;
        }

        // Components list
        const container = document.getElementById("health-components");
        if (!health.available || !health.components || health.components.length === 0) {
            container.innerHTML = '<p class="text-gray-500">Engine not available</p>';
        } else {
            let html = "";
            for (const comp of health.components) {
                const statusClass = "status-" + comp.status;
                const details = comp.details ? '<span class="text-gray-500 ml-2 text-xs">' + escapeHtml(comp.details) + "</span>" : "";
                html += '<div class="flex items-center justify-between">';
                html += '  <span class="text-gray-300">' + escapeHtml(comp.name) + details + "</span>";
                html += '  <span class="' + statusClass + ' font-medium text-xs uppercase">' + escapeHtml(comp.status) + "</span>";
                html += "</div>";
            }
            container.innerHTML = html;
        }

        // Meta info
        const meta = document.getElementById("health-meta");
        if (health.available) {
            meta.textContent = "Version: " + (health.version || "unknown") + " | Uptime: " + formatUptime(health.uptime_seconds || 0);
        } else {
            meta.textContent = "";
        }
    }

    // ── Workers Panel ──────────────────────────────────────────

    function updateWorkers(workersData) {
        const countEl = document.getElementById("worker-count");
        countEl.textContent = workersData.available ? (workersData.available_count + "/" + workersData.total + " available") : "N/A";

        const container = document.getElementById("workers-list");
        if (!workersData.available || !workersData.workers || workersData.workers.length === 0) {
            container.innerHTML = workersData.available
                ? '<p class="text-gray-500">No workers registered</p>'
                : '<p class="text-gray-500">Not Available</p>';
            return;
        }

        let html = "";
        for (const w of workersData.workers) {
            const loadPercent = w.load_percent;
            const barColor = loadPercent >= 100 ? "bg-red-500" : loadPercent >= 75 ? "bg-yellow-500" : "bg-green-500";
            const staleWarning = w.heartbeat_stale
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

    function updateTasks(tasksData) {
        const countEl = document.getElementById("task-count");
        countEl.textContent = tasksData.available ? String(tasksData.total) : "N/A";

        // Status counts
        const countsContainer = document.getElementById("task-status-counts");
        if (tasksData.available && tasksData.status_counts) {
            let html = "";
            for (const [status, count] of Object.entries(tasksData.status_counts)) {
                const badgeClass = status === "completed" ? "bg-green-900 text-green-300"
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

        // Pending info
        const pendingEl = document.getElementById("pending-info");
        if (tasksData.available && tasksData.pending_task_count > 0) {
            pendingEl.textContent = tasksData.pending_task_count + " task(s) queued (night window)";
            pendingEl.classList.remove("hidden");
        } else {
            pendingEl.classList.add("hidden");
        }

        // Task list
        const container = document.getElementById("tasks-list");
        if (!tasksData.available || !tasksData.tasks || tasksData.tasks.length === 0) {
            container.innerHTML = tasksData.available
                ? '<p class="text-gray-500">No tasks</p>'
                : '<p class="text-gray-500">Not Available</p>';
            return;
        }

        let html = "";
        for (const t of tasksData.tasks) {
            const statusBadge = getStatusBadge(t.status);
            html += '<div class="border-l-2 pl-3 ' + getStatusBorderColor(t.status) + '">';
            html += '  <div class="flex items-center justify-between">';
            html += "    <span class=\"truncate max-w-[180px]\" title=\"" + escapeHtml(t.description) + "\">" + escapeHtml(t.description.substring(0, 40)) + "</span>";
            html += "    " + statusBadge;
            html += "  </div>";
            html += '  <div class="text-xs text-gray-500">' + escapeHtml(t.id.substring(0, 8)) + (t.project_id ? " | " + escapeHtml(t.project_id.substring(0, 15)) : "") + "</div>";
            html += "</div>";
        }
        container.innerHTML = html;
    }

    // ── Scheduler Panel ────────────────────────────────────────

    function updateScheduler(schedulerData) {
        const statusEl = document.getElementById("scheduler-status");
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
        const nightWindowEl = document.getElementById("scheduler-night-window");
        if (schedulerData.night_window) {
            const active = schedulerData.night_window.active;
            nightWindowEl.innerHTML = '<span class="text-gray-400">Night Window:</span> '
                + '<span class="' + (active ? "text-yellow-400" : "text-green-400") + '">'
                + (active ? "ACTIVE" : "INACTIVE") + "</span>";
        } else {
            nightWindowEl.innerHTML = "";
        }

        // Jobs list
        const jobsContainer = document.getElementById("scheduler-jobs");
        if (schedulerData.jobs && schedulerData.jobs.length > 0) {
            let html = "";
            for (const job of schedulerData.jobs) {
                const enabledBadge = job.enabled
                    ? '<span class="text-green-400 text-xs">enabled</span>'
                    : '<span class="text-gray-500 text-xs">disabled</span>';
                const schedule = job.cron_expression
                    ? '<span class="text-xs text-gray-400">' + escapeHtml(job.cron_expression) + "</span>"
                    : job.execute_after
                        ? '<span class="text-xs text-gray-400">after ' + escapeHtml(job.execute_after.substring(0, 19)) + "</span>"
                        : "";
                html += '<div class="flex items-center justify-between">';
                html += '  <span class="truncate max-w-[200px]" title="' + escapeHtml(job.description) + '">' + escapeHtml(job.description) + "</span>";
                html += "  " + enabledBadge;
                html += "</div>";
                html += '<div class="text-xs text-gray-500">' + schedule + "</div>";
            }
            jobsContainer.innerHTML = html;
        } else {
            jobsContainer.innerHTML = '<p class="text-gray-500">No scheduled jobs</p>';
        }

        // Execution history
        const historyContainer = document.getElementById("scheduler-history");
        if (schedulerData.execution_history && schedulerData.execution_history.length > 0) {
            let html = "";
            for (const h of schedulerData.execution_history) {
                const statusClass = h.status === "Completed" ? "text-green-400"
                    : h.status === "Failed" ? "text-red-400"
                    : h.status === "Deferred" ? "text-yellow-400"
                    : "text-gray-400";
                html += '<div class="flex items-center justify-between">';
                html += '  <span class="text-gray-400">' + escapeHtml(h.task_id.substring(0, 8)) + "</span>";
                html += '  <span class="' + statusClass + '">' + escapeHtml(h.status) + "</span>";
                html += "</div>";
            }
            historyContainer.innerHTML = html;
        } else {
            historyContainer.innerHTML = '<p class="text-gray-500">No execution history</p>';
        }
    }

    // ── Circuit Breaker / Rate Limiter Panel ───────────────────

    function updateCircuitBreaker(cbData) {
        // Circuit Breaker
        const cbContainer = document.getElementById("circuit-breaker-panel");
        const cb = cbData.circuit_breaker;
        if (!cb || !cb.available) {
            cbContainer.innerHTML = '<p class="text-gray-500">Not Available</p>';
        } else {
            const stateClass = "status-" + cb.state.toLowerCase();
            const stateBadge = "badge-" + cb.state.toLowerCase();
            let html = '<div class="flex items-center justify-between">';
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
        }

        // Rate Limiter
        const rlContainer = document.getElementById("rate-limiter-panel");
        const rl = cbData.rate_limiter;
        if (!rl || !rl.available) {
            rlContainer.innerHTML = '<p class="text-gray-500">Not Available</p>';
        } else {
            let html = '<div class="flex items-center justify-between">';
            html += '  <span class="text-gray-400">RPM Available</span>';
            html += '  <span class="text-gray-300">' + rl.rpm_available + "</span>";
            html += "</div>";
            html += '<div class="flex items-center justify-between">';
            html += '  <span class="text-gray-400">TPM Available</span>';
            html += '  <span class="text-gray-300">' + formatNumber(rl.tpm_available) + "</span>";
            html += "</div>";
            html += '<div class="flex items-center justify-between">';
            html += '  <span class="text-gray-400">Active Requests</span>';
            html += '  <span class="text-gray-300">' + rl.active_count + "</span>";
            html += "</div>";
            html += '<div class="flex items-center justify-between">';
            html += '  <span class="text-gray-400">Total Requests</span>';
            html += '  <span class="text-gray-300">' + rl.total_requests + "</span>";
            html += "</div>";
            // Engine-side rate limiter details
            if (cbData.engine_rate_limiter && cbData.engine_rate_limiter.details) {
                html += '<div class="text-xs text-gray-500 mt-1 pt-1 border-t border-gray-700">' + escapeHtml(cbData.engine_rate_limiter.details) + "</div>";
            }
            rlContainer.innerHTML = html;
        }
    }

    // ── Helpers ────────────────────────────────────────────────

    function escapeHtml(str) {
        if (!str) return "";
        const div = document.createElement("div");
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    function formatUptime(seconds) {
        if (!seconds) return "0s";
        const d = Math.floor(seconds / 86400);
        const h = Math.floor((seconds % 86400) / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
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
        const cls = status === "completed" ? "bg-green-900 text-green-300"
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
        fetch("/dashboard/api/health").then(r => r.json()).then(d => updateHealth(d)).catch(() => {});
        fetch("/dashboard/api/workers").then(r => r.json()).then(d => updateWorkers(d)).catch(() => {});
        fetch("/dashboard/api/tasks").then(r => r.json()).then(d => updateTasks(d)).catch(() => {});
        fetch("/dashboard/api/scheduler").then(r => r.json()).then(d => updateScheduler(d)).catch(() => {});
        fetch("/dashboard/api/circuit-breaker").then(r => r.json()).then(d => updateCircuitBreaker(d)).catch(() => {});
    }

    fetchInitialData();
    connect();
})();
