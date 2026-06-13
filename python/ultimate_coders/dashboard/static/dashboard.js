/**
 * UltimateCoders Dashboard — SSE client + DOM update + interactive operations + task submit.
 *
 * SSE hybrid: 'update' events (5s full snapshot) + 'task_event' events (real-time).
 */

(function () {
    "use strict";

    var STREAM_URL = "/dashboard/api/stream";
    var eventSource = null;
    var connected = false;

    // Interaction log store: keyed by task_id
    var _interactionLog = {};

    // Mermaid init
    if (typeof mermaid !== "undefined") {
        mermaid.initialize({
            startOnLoad: false,
            theme: "dark",
            themeVariables: {
                primaryColor: "#1e293b", primaryBorderColor: "#334155",
                primaryTextColor: "#e2e8f0", lineColor: "#475569",
            },
            flowchart: { useMaxWidth: true, htmlLabels: true, curve: "basis" },
        });
    }

    // ── Confirm Modal ────────────────────────────────────────────

    var _confirmResolve = null;

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

    // ── Toast ──────────────────────────────────────────────────

    function showToast(message, type) {
        var container = document.getElementById("toast-container");
        var toast = document.createElement("div");
        toast.className = "toast toast-" + type;
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 4000);
    }

    // ── POST Action with Confirm ─────────────────────────────────

    window.confirmAction = function (title, message, url) {
        showConfirm(title, message).then(function (confirmed) {
            if (!confirmed) return;
            fetch(url, { method: "POST" })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data.success) showToast(title + " succeeded", "success");
                    else showToast(title + " failed: " + (data.error || "unknown"), "error");
                })
                .catch(function (e) { showToast(title + " failed: " + e.message, "error"); });
        });
    };

    // ── Task Submit ──────────────────────────────────────────────

    window.submitTask = function () {
        var desc = document.getElementById("task-description").value.trim();
        if (!desc) { showToast("Task description is required", "error"); return; }
        var proj = document.getElementById("task-project").value.trim();
        var btn = document.getElementById("submit-task-btn");
        btn.disabled = true;
        btn.textContent = "Submitting...";
        fetch("/dashboard/api/tasks/submit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ description: desc, project_id: proj }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            btn.disabled = false;
            btn.textContent = "Submit Task";
            if (data.success) {
                showToast("Task submitted: " + data.task_id.substring(0, 8), "success");
                document.getElementById("task-description").value = "";
                document.getElementById("task-project").value = "";
                // Init interaction log for this task
                _interactionLog[data.task_id] = [];
                // Auto-scroll to tasks panel
                document.getElementById("tasks-panel")?.scrollIntoView({behavior: "smooth", block: "start"});
            } else {
                showToast("Submit failed: " + (data.error || "unknown"), "error");
            }
        })
        .catch(function (e) {
            btn.disabled = false;
            btn.textContent = "Submit Task";
            showToast("Submit failed: " + e.message, "error");
        });
    };

    // ── Connection Management ──────────────────────────────────

    function connect() {
        if (eventSource) eventSource.close();
        eventSource = new EventSource(STREAM_URL);

        // Full snapshot events (5s periodic)
        eventSource.addEventListener("update", function (event) {
            try {
                var data = JSON.parse(event.data);
                handleUpdate(data);
                setConnected(true);
            } catch (e) { console.error("Failed to parse SSE update:", e); }
        });

        // Real-time task events
        eventSource.addEventListener("task_event", function (event) {
            try {
                var ev = JSON.parse(event.data);
                handleTaskEvent(ev);
                setConnected(true);
            } catch (e) { console.error("Failed to parse SSE task_event:", e); }
        });

        eventSource.addEventListener("error", function () { setConnected(false); });
        eventSource.addEventListener("open", function () { setConnected(true); });
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

    // ── Task Event Handler ───────────────────────────────────────

    function handleTaskEvent(ev) {
        // Add to interaction log
        var tid = ev.task_id;
        if (!_interactionLog[tid]) _interactionLog[tid] = [];
        _interactionLog[tid].push(ev);

        // Also add to event log panel
        updateEventLogFromTaskEvent(ev);

        // If a task detail is currently expanded for this task, refresh interaction
        var detailEl = document.getElementById("task-detail-content-" + tid);
        if (detailEl && detailEl.innerHTML) {
            appendInteractionEntry(tid, ev);
        }
    }

    function updateEventLogFromTaskEvent(ev) {
        var container = document.getElementById("event-log-list");
        var countEl = document.getElementById("event-count");
        var time = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : "--";
        var typeColor = "text-gray-400";
        if (ev.type === "task_submitted") typeColor = "text-blue-400";
        else if (ev.type === "subtask_started") typeColor = "text-yellow-400";
        else if (ev.type === "llm_request") typeColor = "text-purple-400";
        else if (ev.type === "tool_call") typeColor = "text-blue-400";
        else if (ev.type === "tool_result") typeColor = "text-green-400";
        else if (ev.type === "subtask_completed") typeColor = "text-green-400";
        else if (ev.type === "subtask_failed") typeColor = "text-red-400";
        else if (ev.type === "task_completed") typeColor = "text-green-400";

        var dataStr = "";
        if (ev.data) {
            if (ev.data.description) dataStr = escapeHtml(ev.data.description.substring(0, 30));
            else if (ev.data.tool) dataStr = escapeHtml(ev.data.tool);
            else if (ev.data.model) dataStr = escapeHtml(ev.data.model);
            else if (ev.data.summary) dataStr = escapeHtml(ev.data.summary.substring(0, 30));
        }
        var tidShort = ev.task_id ? ev.task_id.substring(0, 8) : "--";

        // Insert at top
        var item = document.createElement("div");
        item.className = "event-item";
        item.innerHTML = '<span class="text-gray-500">' + time + "</span> "
            + '<span class="' + typeColor + ' font-medium">' + escapeHtml(ev.type) + "</span> "
            + '<span class="text-gray-500">' + tidShort + "</span> "
            + '<span class="text-gray-400">' + dataStr + "</span>";

        // Remove "No events" placeholder if present
        var placeholder = container.querySelector("p");
        if (placeholder) container.removeChild(placeholder);

        container.insertBefore(item, container.firstChild);

        // Update count
        var currentCount = parseInt(countEl.textContent) || 0;
        countEl.textContent = String(currentCount + 1);
    }

    function appendInteractionEntry(tid, ev) {
        var container = document.getElementById("interaction-" + tid);
        if (!container) return;

        var time = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : "--";
        var cls = "interaction-entry";
        var stid = ev.subtask_id || "";
        var content = "";

        if (ev.type === "llm_request") {
            cls += " llm-event";
            content = '<span class="text-purple-400">&#9679;</span> LLM request: <span class="text-purple-300 font-medium">' + escapeHtml(ev.data.model || "") + "</span>";
        } else if (ev.type === "subtask_started") {
            cls += " subtask-event";
            content = '<span class="text-yellow-400">&#9679;</span> Subtask started: ' + escapeHtml((ev.data.description || "").substring(0, 40));
        } else if (ev.type === "tool_call") {
            cls += " tool-call";
            content = '<span class="text-blue-400">&#9679;</span> Tool call: <span class="text-blue-300 font-medium">' + escapeHtml(ev.data.tool || "") + "</span>"
                + (ev.data.input_summary ? ' <span class="text-gray-500">' + escapeHtml(ev.data.input_summary.substring(0, 80)) + "</span>" : "");
        } else if (ev.type === "tool_result") {
            cls += " tool-result";
            content = '<span class="text-green-400">&#9679;</span> Tool result: ' + escapeHtml((ev.data.result_summary || "").substring(0, 100));
        } else if (ev.type === "subtask_completed") {
            cls += " subtask-event";
            content = '<span class="text-green-400">&#9679;</span> Subtask completed: ' + escapeHtml((ev.data.summary || "").substring(0, 60));
        } else if (ev.type === "subtask_failed") {
            cls += " subtask-event";
            content = '<span class="text-red-400">&#9679;</span> Subtask failed: ' + escapeHtml((ev.data.error || "").substring(0, 60));
        } else if (ev.type === "task_completed") {
            cls += " subtask-event";
            var tStatus = ev.data.status || "completed";
            content = '<span class="' + (tStatus === "completed" ? "text-green-400" : "text-red-400") + '">&#9679;</span> Task ' + escapeHtml(tStatus);
        } else {
            content = '<span class="text-gray-400">&#9679;</span> ' + escapeHtml(ev.type);
        }

        var entry = document.createElement("div");
        entry.className = cls;
        if (stid) entry.setAttribute("data-subtask-id", stid);
        entry.innerHTML = '<span class="text-gray-500 text-xs">' + time + "</span> " + content;

        // Apply current filter if active
        var selectEl = document.getElementById("interaction-filter-" + tid);
        if (selectEl && selectEl.value && stid !== selectEl.value) {
            entry.style.display = "none";
        }

        container.appendChild(entry);
    }

    // ── Update Handler (full snapshot) ─────────────────────────────

    function handleUpdate(data) {
        var lastUpdate = document.getElementById("last-update");
        if (data.timestamp) lastUpdate.textContent = new Date(data.timestamp).toLocaleTimeString();
        if (data.health) updateHealth(data.health);
        if (data.workers) updateWorkers(data.workers);
        if (data.tasks) updateTasks(data.tasks);
        if (data.scheduler) updateScheduler(data.scheduler);
        if (data.circuit_breaker) updateCircuitBreaker(data.circuit_breaker);
        if (data.events) updateEventLog(data.events);
    }

    // ── Health Panel ───────────────────────────────────────────
    function updateHealth(health) {
        var overall = document.getElementById("health-overall");
        if (!health.available) { overall.textContent = "Unavailable"; overall.className = "text-xs px-2 py-0.5 rounded badge-unavailable"; }
        else { overall.textContent = health.status.toUpperCase(); overall.className = "text-xs px-2 py-0.5 rounded badge-" + health.status; }
        var container = document.getElementById("health-components");
        if (!health.available || !health.components || health.components.length === 0) { container.innerHTML = '<p class="text-gray-500">Engine not available</p>'; }
        else {
            var html = "";
            for (var i = 0; i < health.components.length; i++) {
                var comp = health.components[i];
                html += '<div class="flex items-center justify-between"><span class="text-gray-300">' + escapeHtml(comp.name) + (comp.details ? '<span class="text-gray-500 ml-2 text-xs">' + escapeHtml(comp.details) + "</span>" : "") + "</span><span class=\"status-" + comp.status + " font-medium text-xs uppercase\">" + escapeHtml(comp.status) + "</span></div>";
            }
            container.innerHTML = html;
        }
        var meta = document.getElementById("health-meta");
        meta.textContent = health.available ? "Version: " + (health.version || "unknown") + " | Uptime: " + formatUptime(health.uptime_seconds || 0) : "";
    }

    // ── Workers Panel ──────────────────────────────────────────
    function updateWorkers(wd) {
        document.getElementById("worker-count").textContent = wd.available ? (wd.available_count + "/" + wd.total + " available") : "N/A";
        var container = document.getElementById("workers-list");
        if (!wd.available || !wd.workers || wd.workers.length === 0) { container.innerHTML = wd.available ? '<p class="text-gray-500">No workers</p>' : '<p class="text-gray-500">Not Available</p>'; return; }
        var html = "";
        for (var i = 0; i < wd.workers.length; i++) {
            var w = wd.workers[i], lp = w.load_percent;
            var bc = lp >= 100 ? "bg-red-500" : lp >= 75 ? "bg-yellow-500" : "bg-green-500";
            var sw = w.heartbeat_stale ? ' <span class="text-yellow-400 text-xs">&#9888;</span>' : "";
            html += '<div class="border-l-2 ' + (w.is_available ? "border-green-500" : "border-red-500") + ' pl-3"><div class="flex items-center justify-between"><span>' + escapeHtml(w.id.substring(0, 8)) + sw + "</span><span class=\"text-xs text-gray-400\">" + w.current_load + "/" + w.max_capacity + "</span></div><div class=\"load-bar mt-1\"><div class=\"load-bar-fill " + bc + "\" style=\"width: " + lp + '%"></div></div>' + (w.capabilities && w.capabilities.length > 0 ? '<div class="text-xs text-gray-500 mt-1">' + escapeHtml(w.capabilities.join(", ")) + "</div>" : "") + "</div>";
        }
        container.innerHTML = html;
    }

    // ── Tasks Panel (with detail expansion + interaction + output) ─
    var _taskDataCache = {};

    function updateTasks(td) {
        document.getElementById("task-count").textContent = td.available ? String(td.total) : "N/A";
        var countsContainer = document.getElementById("task-status-counts");
        if (td.available && td.status_counts) {
            var html = "";
            for (var s in td.status_counts) {
                var c = td.status_counts[s];
                var bc = s === "completed" ? "bg-green-900 text-green-300" : s === "failed" ? "bg-red-900 text-red-300" : s === "paused" ? "bg-yellow-900 text-yellow-300" : s === "in_progress" ? "bg-blue-900 text-blue-300" : "bg-gray-700 text-gray-300";
                html += '<span class="text-xs px-2 py-0.5 rounded ' + bc + '">' + escapeHtml(s) + ": " + c + "</span>";
            }
            countsContainer.innerHTML = html;
        } else countsContainer.innerHTML = "";

        var pendingEl = document.getElementById("pending-info");
        var flushBtn = document.getElementById("flush-pending-btn");
        if (td.available && td.pending_task_count > 0) { pendingEl.textContent = td.pending_task_count + " task(s) queued"; pendingEl.classList.remove("hidden"); flushBtn.classList.remove("hidden"); }
        else { pendingEl.classList.add("hidden"); flushBtn.classList.add("hidden"); }

        var container = document.getElementById("tasks-list");
        if (!td.available || !td.tasks || td.tasks.length === 0) { container.innerHTML = td.available ? '<p class="text-gray-500">No tasks</p>' : '<p class="text-gray-500">Not Available</p>'; return; }

        for (var i = 0; i < td.tasks.length; i++) _taskDataCache[td.tasks[i].id] = td.tasks[i];

        var html = "";
        for (var j = 0; j < td.tasks.length; j++) {
            var task = td.tasks[j];
            var sb = getStatusBadge(task.status);
            var ab = "";
            if (task.status === "in_progress" || task.status === "planning") ab = ' <button class="btn-action btn-pause" onclick="confirmAction(\'Pause Task\',\'Pause task ' + escapeHtml(task.id.substring(0, 8)) + '?\',\'/dashboard/api/tasks/' + task.id + '/pause\')">Pause</button>';
            else if (task.status === "paused") ab = ' <button class="btn-action btn-resume" onclick="confirmAction(\'Resume Task\',\'Resume task ' + escapeHtml(task.id.substring(0, 8)) + '?\',\'/dashboard/api/tasks/' + task.id + '/resume\')">Resume</button>';

            html += '<div class="border-l-2 pl-3 ' + getStatusBorderColor(task.status) + ' cursor-pointer" onclick="toggleTaskDetail(\'' + task.id + '\')">';
            html += '<div class="flex items-center justify-between"><span class="truncate max-w-[180px]" title="' + escapeHtml(task.description) + '">' + escapeHtml(task.description.substring(0, 40)) + "</span>" + sb + ab + "</div>";
            html += '<div class="text-xs text-gray-500">' + escapeHtml(task.id.substring(0, 8)) + (task.project_id ? " | " + escapeHtml(task.project_id.substring(0, 15)) : "") + "</div>";
            html += "</div>";
            // Detail expansion
            html += '<div id="task-detail-' + task.id + '" class="task-detail">';
            html += '<div id="task-detail-content-' + task.id + '" class="pl-5 py-2 text-xs text-gray-400">Loading...</div>';
            html += "</div>";
        }
        container.innerHTML = html;
    }

    // ── Task Detail Expansion ────────────────────────────────────

    window.toggleTaskDetail = function (taskId) {
        var el = document.getElementById("task-detail-" + taskId);
        if (!el) return;
        if (el.classList.contains("expanded")) { el.classList.remove("expanded"); return; }
        el.classList.add("expanded");

        var contentEl = document.getElementById("task-detail-content-" + taskId);

        // Build detail content
        var html = '<div class="mb-2"><strong class="text-gray-300">Task:</strong> ' + escapeHtml(taskId.substring(0, 12)) + "</div>";

        // Subtask list section (from cached task data)
        var taskData = _taskDataCache[taskId];
        var subtasks = (taskData && taskData.subtasks) ? taskData.subtasks : [];
        if (subtasks.length > 0) {
            html += '<div class="mb-3"><strong class="text-gray-300">Subtasks:</strong>';
            html += '<div class="space-y-1">';
            for (var si = 0; si < subtasks.length; si++) {
                var st = subtasks[si];
                var stDesc = escapeHtml((st.description || "").substring(0, 50));
                var stBadge = getStatusBadge(st.status || "pending");
                var depInfo = "";
                if (st.depends_on && st.depends_on.length > 0) {
                    var depShorts = [];
                    for (var di = 0; di < st.depends_on.length; di++) {
                        depShorts.push(st.depends_on[di].substring(0, 8));
                    }
                    depInfo = ' <span class="text-gray-500 text-xs">depends: ' + depShorts.join(", ") + "</span>";
                }
                html += '<div class="flex items-center justify-between"><span class="truncate max-w-[200px]">' + stDesc + depInfo + "</span>" + stBadge + "</div>";
            }
            html += "</div></div>";
        }

        // Interaction log section with subtask filter
        html += '<div class="mb-3"><div class="flex items-center justify-between"><strong class="text-gray-300">Interaction Log:</strong>';
        // Subtask filter dropdown
        if (subtasks.length > 1) {
            html += '<select id="interaction-filter-' + taskId + '" onchange="filterInteractionLog(\'' + taskId + '\')" class="text-xs bg-[#0f172a] border border-[#334155] text-gray-300 rounded px-2 py-1">';
            html += '<option value="">All subtasks</option>';
            for (var sfi = 0; sfi < subtasks.length; sfi++) {
                var sfst = subtasks[sfi];
                var sfLabel = escapeHtml((sfst.description || "").substring(0, 25));
                html += '<option value="' + escapeHtml(sfst.id) + '">' + sfLabel + ' (' + sfst.id.substring(0, 8) + ')</option>';
            }
            html += '</select>';
        }
        html += '</div></div>';
        html += '<div id="interaction-' + taskId + '" class="mb-3 max-h-64 overflow-y-auto">';

        // Populate from cached interaction log
        var log = _interactionLog[taskId] || [];
        if (log.length > 0) {
            for (var k = 0; k < log.length; k++) {
                var ev = log[k];
                var time = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : "--";
                var cls = "interaction-entry";
                var stid = ev.subtask_id || "";
                var content = "";
                if (ev.type === "llm_request") { cls += " llm-event"; content = '<span class="text-purple-400">&#9679;</span> LLM request: <span class="text-purple-300 font-medium">' + escapeHtml(ev.data.model || "") + "</span>"; }
                else if (ev.type === "subtask_started") { cls += " subtask-event"; content = '<span class="text-yellow-400">&#9679;</span> Started: ' + escapeHtml((ev.data.description || "").substring(0, 40)); }
                else if (ev.type === "tool_call") { cls += " tool-call"; content = '<span class="text-blue-400">&#9679;</span> Tool: <span class="text-blue-300">' + escapeHtml(ev.data.tool || "") + "</span>"; }
                else if (ev.type === "tool_result") { cls += " tool-result"; content = '<span class="text-green-400">&#9679;</span> Result: ' + escapeHtml((ev.data.result_summary || "").substring(0, 100)); }
                else if (ev.type === "subtask_completed") { cls += " subtask-event"; content = '<span class="text-green-400">&#9679;</span> Completed: ' + escapeHtml((ev.data.summary || "").substring(0, 60)); }
                else if (ev.type === "subtask_failed") { cls += " subtask-event"; content = '<span class="text-red-400">&#9679;</span> Failed: ' + escapeHtml((ev.data.error || "").substring(0, 60)); }
                else if (ev.type === "task_completed") { cls += " subtask-event"; var tSt = ev.data.status || "completed"; content = '<span class="' + (tSt === "completed" ? "text-green-400" : "text-red-400") + '">&#9679;</span> Task ' + escapeHtml(tSt); }
                else { content = '<span class="text-gray-400">&#9679;</span> ' + escapeHtml(ev.type); }
                html += '<div class="' + cls + '" data-subtask-id="' + escapeHtml(stid) + '"><span class="text-gray-500 text-xs">' + time + "</span> " + content + "</div>";
            }
        } else {
            html += '<p class="text-gray-500">No interaction events yet</p>';
        }
        html += "</div>";

        // Output files section (from events with modified_files) — enhanced with icons
        var outputFiles = [];
        for (var m = 0; m < log.length; m++) {
            if (log[m].type === "subtask_completed" && log[m].data && log[m].data.modified_files) {
                for (var n = 0; n < log[m].data.modified_files.length; n++) {
                    var mf = log[m].data.modified_files[n];
                    outputFiles.push({path: mf.path, type: mf.type, _source_subtask: log[m].subtask_id || ""});
                }
            }
        }
        if (outputFiles.length > 0) {
            html += '<div class="mb-2"><strong class="text-gray-300">Output Files:</strong> <span class="text-xs text-gray-500">' + outputFiles.length + ' file(s) changed</span></div>';
            html += '<div class="space-y-1">';
            for (var p = 0; p < outputFiles.length; p++) {
                var f = outputFiles[p];
                var changeIcon, changeLabel, changeColor, changeBg;
                if (f.type === "created") { changeIcon = "+"; changeLabel = "CREATED"; changeColor = "text-green-400"; changeBg = "bg-green-900"; }
                else if (f.type === "modified") { changeIcon = "~"; changeLabel = "MODIFIED"; changeColor = "text-yellow-400"; changeBg = "bg-yellow-900"; }
                else if (f.type === "deleted") { changeIcon = "-"; changeLabel = "DELETED"; changeColor = "text-red-400"; changeBg = "bg-red-900"; }
                else { changeIcon = "?"; changeLabel = "UNKNOWN"; changeColor = "text-gray-400"; changeBg = "bg-gray-700"; }
                html += '<div class="flex items-center gap-2 py-1 px-2 rounded ' + changeBg + ' bg-opacity-30">';
                html += '<span class="' + changeColor + ' font-mono font-bold text-sm w-4 text-center">' + changeIcon + "</span>";
                html += '<span class="text-gray-200 text-xs font-mono truncate flex-1" title="' + escapeHtml(f.path) + '">' + escapeHtml(f.path) + "</span>";
                html += '<span class="' + changeColor + ' text-[10px] font-medium px-1.5 py-0.5 rounded">' + changeLabel + "</span>";
                if (f._source_subtask) html += '<span class="text-gray-600 text-[10px]">' + f._source_subtask.substring(0, 8) + "</span>";
                html += "</div>";
            }
            html += "</div>";
        }

        // Mermaid DAG (if subtasks have dependencies)
        var dagSubtasks = (taskData && taskData.subtasks) ? taskData.subtasks : [];
        var hasDeps = false;
        for (var di2 = 0; di2 < dagSubtasks.length; di2++) {
            if (dagSubtasks[di2].depends_on && dagSubtasks[di2].depends_on.length > 0) { hasDeps = true; break; }
        }
        if (dagSubtasks.length > 0 && hasDeps && typeof mermaid !== "undefined") {
            // Build subtask id -> short label map
            var stIdMap = {};
            for (var mi = 0; mi < dagSubtasks.length; mi++) {
                var stItem = dagSubtasks[mi];
                var shortId = "s" + (mi + 1);
                stIdMap[stItem.id] = shortId;
            }
            var graphDef = "graph LR\n";
            for (var mj = 0; mj < dagSubtasks.length; mj++) {
                var stNode = dagSubtasks[mj];
                var nodeId = stIdMap[stNode.id];
                var nodeLabel = escapeHtml((stNode.description || "").substring(0, 25).replace(/"/g, "'"));
                graphDef += '  ' + nodeId + '["' + nodeLabel + '"]\n';
                if (stNode.depends_on && stNode.depends_on.length > 0) {
                    for (var dk = 0; dk < stNode.depends_on.length; dk++) {
                        var depId = stIdMap[stNode.depends_on[dk]];
                        if (depId) graphDef += "  " + depId + " --> " + nodeId + "\n";
                    }
                }
            }
            html += '<div class="mb-2"><strong class="text-gray-300">Subtask DAG:</strong></div>';
            html += '<div id="mermaid-' + taskId + '" class="mermaid-src">' + escapeHtml(graphDef) + "</div>";
        } else if (dagSubtasks.length > 0) {
            html += '<div id="mermaid-' + taskId + '"></div>';
        } else {
            html += '<div id="mermaid-' + taskId + '"></div>';
        }

        contentEl.innerHTML = html;

        // Render Mermaid DAG if present
        if (dagSubtasks.length > 0 && hasDeps && typeof mermaid !== "undefined") {
            var mermaidEl = document.getElementById("mermaid-" + taskId);
            if (mermaidEl) {
                try {
                    var graphId = "mermaid-graph-" + taskId.substring(0, 8);
                    mermaid.render(graphId, graphDef).then(function (result) {
                        mermaidEl.innerHTML = result.svg;
                    }).catch(function (e) {
                        mermaidEl.innerHTML = '<p class="text-gray-500">DAG render error</p>';
                    });
                } catch (e) {
                    mermaidEl.innerHTML = '<p class="text-gray-500">DAG render error</p>';
                }
            }
        }
    };

    // ── Interaction Log Subtask Filter ────────────────────────────

    window.filterInteractionLog = function (taskId) {
        var selectEl = document.getElementById("interaction-filter-" + taskId);
        var container = document.getElementById("interaction-" + taskId);
        if (!selectEl || !container) return;

        var filterValue = selectEl.value;
        var entries = container.querySelectorAll(".interaction-entry");

        for (var i = 0; i < entries.length; i++) {
            var entry = entries[i];
            var entrySubtaskId = entry.getAttribute("data-subtask-id") || "";
            if (!filterValue || entrySubtaskId === filterValue) {
                entry.style.display = "";
            } else {
                entry.style.display = "none";
            }
        }
    };

    // ── Scheduler Panel ────────────────────────────────────────
    function updateScheduler(sd) {
        var statusEl = document.getElementById("scheduler-status");
        if (!sd.available) { statusEl.textContent = "Not Available"; statusEl.className = "text-xs px-2 py-0.5 rounded badge-unavailable"; document.getElementById("scheduler-night-window").innerHTML = ""; document.getElementById("scheduler-jobs").innerHTML = '<p class="text-gray-500">No scheduler</p>'; document.getElementById("scheduler-history").innerHTML = ""; return; }
        statusEl.textContent = sd.is_running ? "RUNNING" : "STOPPED"; statusEl.className = "text-xs px-2 py-0.5 rounded badge-" + (sd.is_running ? "ok" : "degraded");
        var nwEl = document.getElementById("scheduler-night-window");
        if (sd.night_window) { var a = sd.night_window.active; nwEl.innerHTML = '<span class="text-gray-400">Night Window:</span> <span class="' + (a ? "text-yellow-400" : "text-green-400") + '">' + (a ? "ACTIVE" : "INACTIVE") + "</span>"; } else nwEl.innerHTML = "";
        var jc = document.getElementById("scheduler-jobs");
        if (sd.jobs && sd.jobs.length > 0) {
            var h = "";
            for (var i = 0; i < sd.jobs.length; i++) {
                var job = sd.jobs[i];
                var tb = job.enabled ? ' <button class="btn-action btn-trigger" onclick="confirmAction(\'Trigger Job\',\'Trigger ' + escapeHtml(job.description.substring(0, 30)) + '?\',\'/dashboard/api/scheduler/jobs/' + job.id + '/trigger\')">Trigger</button>' : "";
                var sch = job.cron_expression ? '<span class="text-xs text-gray-400">' + escapeHtml(job.cron_expression) + "</span>" : job.execute_after ? '<span class="text-xs text-gray-400">after ' + escapeHtml(job.execute_after.substring(0, 19)) + "</span>" : "";
                h += '<div class="flex items-center justify-between"><span class="truncate max-w-[200px]">' + escapeHtml(job.description) + "</span>" + (job.enabled ? '<span class="text-green-400 text-xs">enabled</span>' : '<span class="text-gray-500 text-xs">disabled</span>') + tb + "</div><div class=\"text-xs text-gray-500\">" + sch + "</div>";
            }
            jc.innerHTML = h;
        } else jc.innerHTML = '<p class="text-gray-500">No scheduled jobs</p>';
        var hc = document.getElementById("scheduler-history");
        if (sd.execution_history && sd.execution_history.length > 0) {
            var hh = "";
            for (var j = 0; j < sd.execution_history.length; j++) { var e = sd.execution_history[j]; hh += '<div class="flex items-center justify-between"><span class="text-gray-400">' + escapeHtml(e.task_id.substring(0, 8)) + "</span><span class=\"" + (e.status === "Completed" ? "text-green-400" : e.status === "Failed" ? "text-red-400" : "text-gray-400") + '">' + escapeHtml(e.status) + "</span></div>"; }
            hc.innerHTML = hh;
        } else hc.innerHTML = '<p class="text-gray-500">No execution history</p>';
    }

    // ── Circuit Breaker / Rate Limiter ───────────────────────────
    function updateCircuitBreaker(cbData) {
        var cbC = document.getElementById("circuit-breaker-panel"), cb = cbData.circuit_breaker, rb = document.getElementById("cb-reset-btn");
        if (!cb || !cb.available) { cbC.innerHTML = '<p class="text-gray-500">Not Available</p>'; rb.classList.add("hidden"); }
        else {
            var h = '<div class="flex items-center justify-between"><span class="text-gray-400">State</span><span class="text-xs px-2 py-0.5 rounded badge-' + cb.state.toLowerCase() + '">' + escapeHtml(cb.state) + "</span></div>";
            h += '<div class="flex items-center justify-between"><span class="text-gray-400">Failures</span><span class="' + (cb.failure_count > 0 ? "text-red-400" : "text-gray-300") + '">' + cb.failure_count + "</span></div>";
            h += '<div class="flex items-center justify-between"><span class="text-gray-400">Total Calls</span><span class="text-gray-300">' + cb.total_calls + "</span></div>";
            h += '<div class="flex items-center justify-between"><span class="text-gray-400">Rejected</span><span class="' + (cb.total_rejected > 0 ? "text-yellow-400" : "text-gray-300") + '">' + cb.total_rejected + "</span></div>";
            cbC.innerHTML = h;
            if (cb.state.toLowerCase() === "open" || cb.state.toLowerCase() === "half_open") rb.classList.remove("hidden"); else rb.classList.add("hidden");
        }
        var rlC = document.getElementById("rate-limiter-panel"), rl = cbData.rate_limiter;
        if (!rl || !rl.available) rlC.innerHTML = '<p class="text-gray-500">Not Available</p>';
        else {
            var rh = '<div class="flex items-center justify-between"><span class="text-gray-400">RPM</span><span class="text-gray-300">' + rl.rpm_available + "</span></div>";
            rh += '<div class="flex items-center justify-between"><span class="text-gray-400">TPM</span><span class="text-gray-300">' + formatNumber(rl.tpm_available) + "</span></div>";
            rh += '<div class="flex items-center justify-between"><span class="text-gray-400">Active</span><span class="text-gray-300">' + rl.active_count + "</span></div>";
            rh += '<div class="flex items-center justify-between"><span class="text-gray-400">Total</span><span class="text-gray-300">' + rl.total_requests + "</span></div>";
            rlC.innerHTML = rh;
        }
    }

    // ── Event Log Panel ─────────────────────────────────────────
    function updateEventLog(events) {
        var countEl = document.getElementById("event-count");
        var container = document.getElementById("event-log-list");
        if (!events || events.length === 0) { countEl.textContent = "0"; container.innerHTML = '<p class="text-gray-500">No events</p>'; return; }
        countEl.textContent = String(events.length);
        var html = "";
        for (var i = 0; i < events.length; i++) {
            var ev = events[i];
            var time = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : "--";
            var tc = "text-gray-400";
            if (ev.type === "task_pause" || ev.type === "circuit_breaker_reset") tc = "text-yellow-400";
            else if (ev.type === "task_resume" || ev.type === "scheduler_trigger") tc = "text-green-400";
            else if (ev.type === "task_submitted") tc = "text-blue-400";
            else if (ev.type === "task_completed") tc = "text-green-400";
            var dp = [];
            if (ev.details) { for (var key in ev.details) { var v = ev.details[key]; if (typeof v === "string" && v.length > 12) v = v.substring(0, 12) + "..."; dp.push(escapeHtml(key) + "=" + escapeHtml(String(v))); } }
            var ds = dp.length > 0 ? " <span class='text-gray-500'>" + dp.join(", ") + "</span>" : "";
            html += '<div class="event-item"><span class="text-gray-500">' + time + "</span> <span class=\"" + tc + ' font-medium">' + escapeHtml(ev.type) + "</span>" + ds + "</div>";
        }
        container.innerHTML = html;
    }

    // ── Helpers ────────────────────────────────────────────────
    function escapeHtml(s) { if (!s) return ""; var d = document.createElement("div"); d.appendChild(document.createTextNode(s)); return d.innerHTML; }
    function formatUptime(s) { if (!s) return "0s"; var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60; if (d > 0) return d + "d " + h + "h"; if (h > 0) return h + "h " + m + "m"; if (m > 0) return m + "m " + sec + "s"; return sec + "s"; }
    function formatNumber(n) { if (n >= 1000000) return (n / 1000000).toFixed(1) + "M"; if (n >= 1000) return (n / 1000).toFixed(1) + "K"; return String(Math.round(n)); }
    function getStatusBadge(st) { var c = st === "completed" ? "bg-green-900 text-green-300" : st === "failed" ? "bg-red-900 text-red-300" : st === "paused" ? "bg-yellow-900 text-yellow-300" : st === "in_progress" ? "bg-blue-900 text-blue-300" : "bg-gray-700 text-gray-300"; return '<span class="text-xs px-1.5 py-0.5 rounded ' + c + '">' + escapeHtml(st) + "</span>"; }
    function getStatusBorderColor(st) { if (st === "completed") return "border-green-700"; if (st === "failed") return "border-red-700"; if (st === "paused") return "border-yellow-700"; if (st === "in_progress") return "border-blue-700"; return "border-gray-600"; }

    // ── Initialize ─────────────────────────────────────────────
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
