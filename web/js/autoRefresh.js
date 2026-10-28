import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";
import { ChangeTracker } from "/scripts/changeTracker.js";

const BACKUP_KEY = "AccountManager.RefreshBackup";
const SEEN_KEY = "AccountManager.RefreshRevision";
let loadedRevision = new URL(import.meta.url).searchParams.get("am_revision");
let busy = false;
let pending = false;
let reloading = false;
let paused = false;
let disconnected = false;
let connectionEpoch = 0;
let retryTimer;
let connectionToast;

function readStored(key) {
    const value = sessionStorage.getItem(key);
    return value ? JSON.parse(value) : null;
}

function pauseRefresh(message, error) {
    paused = true;
    console.error("[Account Manager] Automatic refresh paused", error);
    app.extensionManager.toast.add({
        severity: "warn", summary: "自动刷新已暂停", detail: message, life: 0,
    });
}

async function snapshotWorkflows(info) {
    const store = app.extensionManager.workflow;
    for (const item of store.openWorkflows) {
        if (item !== store.activeWorkflow && !item.activeState) await item.load();
    }
    const active = store.activeWorkflow;
    const workflow = app.rootGraph.serialize();
    const open = store.openWorkflows;
    const workflows = open.map(item => ({
        path: item.path,
        name: item.filename,
        data: item === active ? workflow : item.activeState,
    }));
    if (!workflows.length) workflows.push({ name: "Workflow", data: workflow });
    if (workflows.some(item => !item.data || !Array.isArray(item.data.nodes))) {
        throw new Error("An open workflow has no serializable graph");
    }
    return {
        user_id: info.user_id,
        active: Math.max(0, open.indexOf(active)), workflows,
    };
}

async function restoreWorkflows(info) {
    const backup = readStored(BACKUP_KEY);
    if (!backup) return;
    if (backup.user_id !== info.user_id) {
        sessionStorage.removeItem(BACKUP_KEY);
        return;
    }
    const store = app.extensionManager.workflow;
    for (const item of store.openWorkflows) {
        if (item !== store.activeWorkflow && !item.activeState) await item.load();
    }
    const restored = [];
    for (const saved of backup.workflows) {
        const available = store.openWorkflows.filter(item => !restored.includes(item));
        const graphData = item => item === store.activeWorkflow ? app.rootGraph.serialize() : item.activeState;
        const existing = saved.path
            ? store.getWorkflowByPath(saved.path)
            : available.find(item => saved.data.id && graphData(item)?.id === saved.data.id)
                || available.find(item => ChangeTracker.graphEqual(graphData(item), saved.data));
        if (existing && !existing.activeState) await existing.load();
        if (existing && available.includes(existing) && ChangeTracker.graphEqual(graphData(existing), saved.data)) {
            restored.push(existing);
            continue;
        }
        // Keep the original path and tab instead of creating a renamed duplicate.
        const previous = store.activeWorkflow;
        const initialState = existing?.changeTracker.initialState;
        const target = existing || (saved.path?.startsWith("workflows/")
            ? store.createTemporary(saved.path.slice("workflows/".length), saved.data)
            : `${saved.name} (刷新恢复)`);
        const result = await app.loadGraphData(saved.data, true, true, target);
        if (result === false || !store.activeWorkflow || (typeof target !== "string" ? store.activeWorkflow.path !== target.path : store.activeWorkflow === previous)) {
            throw new Error("The recovered workflow could not be opened");
        }
        if (existing) {
            existing.changeTracker.initialState = initialState;
            existing.changeTracker.updateModified();
        }
        restored.push(store.activeWorkflow);
    }
    for (let index = 0; index < restored.length; index++) {
        const current = store.openWorkflows.indexOf(restored[index]);
        if (current !== index) store.reorderWorkflows(current, index);
    }
    const selected = restored[backup.active];
    if (selected && selected !== store.activeWorkflow) {
        const initialState = selected.changeTracker.initialState;
        const result = await app.loadGraphData(selected.activeState, true, true, selected);
        if (result === false || store.activeWorkflow !== selected) {
            throw new Error("The active workflow could not be restored");
        }
        selected.changeTracker.initialState = initialState;
        selected.changeTracker.updateModified();
    }
    sessionStorage.removeItem(BACKUP_KEY);
}

async function checkForUpdate() {
    if (reloading || paused || disconnected) return;
    if (busy) {
        pending = true;
        return;
    }
    if (!app.isGraphReady || app.extensionManager.workflow.isBusy || app.extensionManager.workflow.isSyncLoading || app.configuringGraph) {
        clearTimeout(retryTimer);
        retryTimer = setTimeout(checkForUpdate, 500);
        return;
    }
    busy = true;
    const epoch = connectionEpoch;
    try {
        let info;
        try {
            const response = await api.fetchApi("/am/frontend-version", {
                cache: "no-store", signal: AbortSignal.timeout(5000),
            });
            if (!response.ok) return;
            info = await response.json();
        } catch {
            return; // A stopped server or expired login must not trigger a reload.
        }
        if (epoch !== connectionEpoch || disconnected) return;
        if (!/^\d+$/.test(info.revision) || !info.user_id) return;
        try {
            await restoreWorkflows(info);
        } catch (error) {
            if (epoch !== connectionEpoch || disconnected) return;
            pauseRefresh("工作流恢复未完成，备份已保留。请先检查并保存当前工作流。", error);
            return;
        }
        if (epoch !== connectionEpoch || disconnected) return;
        let seen;
        try {
            seen = readStored(SEEN_KEY);
        } catch (error) {
            pauseRefresh("无法读取页面暂存空间，请先保存工作流后手动刷新。", error);
            return;
        }
        if (!/^\d+$/.test(loadedRevision)) loadedRevision = info.revision;
        let baseline = BigInt(loadedRevision);
        if (seen?.user_id === info.user_id && /^\d+$/.test(seen.revision)) {
            const previous = BigInt(seen.revision);
            if (previous > baseline) baseline = previous;
        }
        if (BigInt(info.revision) <= baseline) return;
        try {
            const backup = await snapshotWorkflows(info);
            if (epoch !== connectionEpoch || disconnected) return;
            sessionStorage.setItem(BACKUP_KEY, JSON.stringify(backup));
            sessionStorage.setItem(SEEN_KEY, JSON.stringify({ revision: info.revision, user_id: info.user_id }));
        } catch (error) {
            if (epoch !== connectionEpoch || disconnected) return;
            const message = error?.name === "QuotaExceededError"
                ? "浏览器暂存空间不足，自动刷新已暂停。请保存打开的工作流后手动刷新。"
                : "工作流备份未完成，自动刷新已暂停。请保存打开的工作流后手动刷新。";
            pauseRefresh(message, error);
            return;
        }
        reloading = true;
        // Change only the in-memory preference; the saved setting returns after reload.
        app.ui.settings.settingsValues["Comfy.Window.UnloadConfirmation"] = false;
        window.location.reload();
    } finally {
        busy = false;
        if (pending && !reloading) {
            pending = false;
            clearTimeout(retryTimer);
            retryTimer = setTimeout(checkForUpdate, 0);
        }
    }
}

app.registerExtension({
    name: "ComfyUI.AccountManager.AutoRefresh",
    setup() {
        // Replace the built-in reconnect toast for this page only (do not save the setting).
        app.ui.settings.settingsValues["Comfy.Toast.DisableReconnectingToast"] = true;
        const onDisconnect = () => {
            if (!disconnected) {
                connectionToast = {
                    severity: "info", summary: "服务器连接已断开，正在等待恢复", life: 0, closable: false,
                };
                app.extensionManager.toast.add(connectionToast);
            }
            disconnected = true;
            connectionEpoch++;
            clearTimeout(retryTimer);
        };
        api.addEventListener("reconnecting", onDisconnect);
        window.addEventListener("offline", onDisconnect);
        api.addEventListener("reconnected", () => {
            disconnected = false;
            if (connectionToast) {
                app.extensionManager.toast.remove(connectionToast);
                connectionToast = undefined;
            }
            return checkForUpdate();
        });
        window.addEventListener("online", checkForUpdate);
        window.addEventListener("pageshow", checkForUpdate);
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible") checkForUpdate();
        });
        retryTimer = setTimeout(checkForUpdate, 100);
    },
    afterLoadGraph() {
        clearTimeout(retryTimer);
        retryTimer = setTimeout(checkForUpdate, 100);
    },
});
