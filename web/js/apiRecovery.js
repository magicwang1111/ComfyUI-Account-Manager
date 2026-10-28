import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

async function showRecoveredVideos(promptId) {
    const job = await api.getJobDetail(promptId);
    const videos = Object.entries(job?.outputs || {})
        .filter(([key]) => key.startsWith("api_recovery_"))
        .flatMap(([, output]) => output.video || []);
    if (!videos.length) throw new Error("Recovered videos are unavailable");

    const dialog = document.createElement("dialog");
    dialog.className = "am-recovery-dialog";
    const title = document.createElement("h2");
    title.textContent = "API 结果已恢复";
    const description = document.createElement("p");
    description.textContent = "原工作流已中断，后续节点未执行。以下是从原 API 任务取回的视频。";
    const close = document.createElement("button");
    close.textContent = "关闭";
    close.addEventListener("click", () => dialog.close());
    dialog.append(title, description, close);
    for (const reference of videos) {
        const video = document.createElement("video");
        video.controls = true;
        video.preload = "metadata";
        video.src = api.apiURL("/view?" + new URLSearchParams({
            filename: reference.filename, subfolder: reference.subfolder || "", type: reference.type || "output",
        }));
        dialog.append(video);
    }
    dialog.addEventListener("close", () => {
        for (const video of dialog.querySelectorAll("video")) {
            video.pause();
            video.removeAttribute("src");
            video.load();
        }
        dialog.remove();
    }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
}

app.registerExtension({
    name: "ComfyUI.AccountManager.APIRecovery",
    setup() {
        const style = document.createElement("style");
        style.textContent = `
            .am-recovered-video-button { position:absolute; right:4px; bottom:2px; z-index:1; padding:3px 7px; border:1px solid #6199ca; border-radius:5px; background:#223e57; color:#fff; font-size:11px; cursor:pointer; }
            .am-recovery-dialog { margin:auto; padding:20px; width:min(900px,90vw); max-height:90vh; overflow:auto; border:1px solid #666; border-radius:10px; background:var(--comfy-menu-bg,#202020); color:var(--input-text,#eee); }
            .am-recovery-dialog::backdrop { background:#0009; }
            .am-recovery-dialog h2 { font-size:20px; margin:0 0 8px; }
            .am-recovery-dialog p { margin:0 55px 14px 0; }
            .am-recovery-dialog > button { position:absolute; top:20px; right:20px; cursor:pointer; }
            .am-recovery-dialog video { display:block; width:100%; max-height:65vh; margin-top:12px; }
        `;
        document.head.append(style);
        const recovered = new Set();
        let frame;
        const updateCards = () => {
            frame = undefined;
            for (const card of document.querySelectorAll("[data-job-id]")) {
                const promptId = card.dataset.jobId;
                const existing = card.querySelector(".am-recovered-video-button");
                if (!recovered.has(promptId)) {
                    existing?.remove();
                    continue;
                }
                if (existing) continue;
                card.style.position = "relative";
                const button = document.createElement("button");
                button.className = "am-recovered-video-button";
                button.textContent = "查看已恢复视频";
                button.addEventListener("click", async event => {
                    event.stopPropagation();
                    button.disabled = true;
                    try {
                        await showRecoveredVideos(promptId);
                    } catch (error) {
                        console.error("[Account Manager] Recovered video could not be opened", error);
                        app.extensionManager.toast.add({ severity: "warn", summary: "无法读取已恢复视频，请稍后重试", life: 5000 });
                    } finally {
                        button.disabled = false;
                    }
                });
                card.append(button);
            }
        };
        const scheduleUpdate = () => { frame ??= requestAnimationFrame(updateCards); };
        const fetchApi = api.fetchApi;
        api.fetchApi = async function (route, ...args) {
            const response = await fetchApi.call(this, route, ...args);
            if (response.ok && /^\/(?:api\/)?jobs(?:\?|$)/.test(route)) {
                response.clone().json().then(({ jobs }) => {
                    for (const job of jobs) {
                        const available = job.api_result_recovered || (
                            job.preview_output?.nodeId?.startsWith("api_recovery_") && job.preview_output.mediaType === "video"
                        );
                        if (available) recovered.add(job.id);
                        else recovered.delete(job.id);
                    }
                    scheduleUpdate();
                }).catch(() => {});
            }
            return response;
        };
        new MutationObserver(scheduleUpdate).observe(document.body, { childList: true, subtree: true });
        // The queue may have loaded before extension setup completed.
        api.getHistory().catch(() => {});
    },
});
