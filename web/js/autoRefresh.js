import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

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
    const active = store.activeWorkflow;
    const { workflow } = await app.graphToPrompt();
    const open = store.openWorkflows;
    const workflows = open.map(item => ({
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
    const activeGraph = (await app.graphToPrompt()).workflow;
    const active = store.activeWorkflow;
    const restored = [];
    for (const saved of backup.workflows) {
        const serialized = JSON.stringify(saved.data);
        const existing = store.openWorkflows.find(item =>
            !restored.includes(item) && JSON.stringify(item === active ? activeGraph : item.activeState) === serialized);
        if (existing) {
            restored.push(existing);
            continue;
        }
        // Recover a local copy; never overwrite a workflow saved on the server.
        const previous = store.activeWorkflow;
        const result = await app.loadGraphData(saved.data, true, true, `${saved.name} (刷新恢复)`);
        if (result === false || !store.activeWorkflow || store.activeWorkflow === previous) {
            throw new Error("The recovered workflow could not be opened");
        }
        restored.push(store.activeWorkflow);
    }
    const selected = restored[backup.active];
    if (selected && selected !== store.activeWorkflow) {
        const result = await app.loadGraphData(selected.activeState, true, true, selected);
        if (result === false || store.activeWorkflow !== selected) {
            throw new Error("The active workflow could not be restored");
        }
    }
    sessionStorage.removeItem(BACKUP_KEY);
}

async function checkForUpdate() {
    if (reloading || paused || disconnected) return;
    if (busy) {
        pending = true;
        return;
    }
    if (!app.isGraphReady || app.extensionManager.workflow.isBusy || app.configuringGraph) {
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
            pauseRefresh("无法安全暂存工作流，请先保存后手动刷新。", error);
            return;
        }
        reloading = true;
        // Change only the in-memory preference; the saved setting returns after reload.
        app.extensionManager.setting.settings["Comfy.Window.UnloadConfirmation"] = false;
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
        app.extensionManager.setting.settings["Comfy.Toast.DisableReconnectingToast"] = true;
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
