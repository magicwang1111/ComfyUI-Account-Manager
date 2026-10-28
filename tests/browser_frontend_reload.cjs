const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const baseURL = process.env.COMFY_TEST_URL || 'http://127.0.0.1:8200';
const source = fs.readFileSync(path.join(__dirname, '../web/js/autoRefresh.js'), 'utf8');
const backupKey = 'AccountManager.RefreshBackup';
const seenKey = 'AccountManager.RefreshRevision';

async function harness(browser) {
    const context = await browser.newContext();
    context.setDefaultTimeout(15000);
    const state = { served: '100', current: '100', user: 'user-a', status: 200, checks: 0 };
    const sockets = [];
    await context.routeWebSocket('**/ws*', socket => {
        if (state.disconnected) return socket.close();
        socket.connectToServer();
        sockets.push(socket);
    });
    await context.route('**/extensions', async route => {
        const response = await route.fetch();
        const extensions = await response.json();
        extensions.push(`/extensions/ComfyUI-Account-Manager/js/autoRefresh.js?am_revision=${state.served}`);
        await route.fulfill({ json: extensions, headers: { 'Cache-Control': 'no-store' } });
    });
    await context.route('**/extensions/ComfyUI-Account-Manager/js/autoRefresh.js*', route =>
        route.fulfill({ contentType: 'application/javascript', body: source, headers: { 'Cache-Control': 'no-cache' } }));
    await context.route('**/am/frontend-version', route => {
        state.checks++;
        return route.fulfill({ status: state.status, json: { revision: state.current, user_id: state.user } });
    });
    const page = await context.newPage();
    let navigations = 0;
    const errors = [];
    page.on('request', request => {
        if (request.isNavigationRequest() && request.frame() === page.mainFrame()) navigations++;
    });
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', async dialog => {
        errors.push('Unexpected browser confirmation: ' + dialog.type());
        await dialog.dismiss();
    });
    await page.goto(baseURL);
    await page.waitForFunction(() => window.comfyAPI?.app?.app?.extensionManager?.workflow?.activeWorkflow);
    await page.waitForFunction(() => window.comfyAPI.app.app.isGraphReady);
    while (!state.checks) await page.waitForTimeout(50);
    return { context, page, state, errors, sockets, navigations: () => navigations };
}

async function reconnect(page) {
    await page.evaluate(async () => {
        const { api } = await import('/scripts/api.js');
        api.dispatchEvent(new Event('reconnected'));
    });
}

async function openDraft(page, name, filename) {
    await page.evaluate(async ({ name, filename }) => {
        const { app } = await import('/scripts/app.js');
        await app.loadGraphData({ last_node_id: 0, last_link_id: 0, nodes: [], links: [], groups: [], config: {}, extra: {}, version: 0.4 }, true, true, name);
        const node = LiteGraph.createNode('LoadImage');
        app.graph.add(node);
        node.widgets.find(w => w.name === 'image').value = filename;
        app.graph.change();
    }, { name, filename });
}

async function draftImages(page) {
    return page.evaluate(async () => {
        const { app } = await import('/scripts/app.js');
        const store = app.extensionManager.workflow;
        return store.openWorkflows.flatMap(item => {
            const data = item === store.activeWorkflow ? app.rootGraph.serialize() : item.activeState;
            return (data?.nodes || []).filter(n => n.type === 'LoadImage').map(n => n.widgets_values[0]);
        });
    });
}

(async () => {
    const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined });
    try {
        const connection = await harness(browser);
        assert.equal(await connection.page.evaluate(() =>
            window.comfyAPI.app.app.extensionManager.setting.get('Comfy.Toast.DisableReconnectingToast')), true);
        const connectionBefore = connection.navigations();
        const waiting = connection.page.getByText('服务器连接已断开，正在等待恢复', { exact: true });
        for (let cycle = 0; cycle < 2; cycle++) {
            await connection.page.evaluate(async () => {
                const { api } = await import('/scripts/api.js');
                api.dispatchEvent(new Event('reconnecting'));
                api.dispatchEvent(new Event('reconnecting'));
            });
            await waiting.waitFor();
            assert.equal(await waiting.count(), 1);
            await reconnect(connection.page);
            await waiting.waitFor({ state: 'hidden' });
        }
        connection.state.disconnected = true;
        await connection.sockets.at(-1).close({ code: 1001, reason: 'Test server shutdown' });
        await waiting.waitFor();
        await connection.page.waitForTimeout(6000);
        assert.equal(await waiting.count(), 1);
        assert.equal(await connection.page.getByText('Reconnecting', { exact: true }).count(), 0);
        assert.equal(await connection.page.getByText('自动刷新已暂停', { exact: true }).count(), 0);
        connection.state.disconnected = false;
        await waiting.waitFor({ state: 'hidden' });
        assert.equal(connection.navigations(), connectionBefore);
        assert.deepEqual(connection.errors, []);
        await connection.context.close();
        console.log('PASS: WebSocket disconnect shows one Chinese waiting toast and reconnect removes it');

        const h = await harness(browser);
        await openDraft(h.page, 'one', 'unsaved-one.png');
        await openDraft(h.page, 'two', 'unsaved-two.png');
        const before = h.navigations();
        h.state.served = h.state.current = '200';
        await Promise.all([h.page.waitForEvent('load'), reconnect(h.page)]);
        await h.page.waitForFunction(key => sessionStorage.getItem(key) === null && window.comfyAPI?.app?.app?.isGraphReady && document.querySelector('body'), backupKey);
        const images = await draftImages(h.page);
        assert(images.includes('unsaved-one.png'), 'Inactive unsaved draft was lost');
        assert(images.includes('unsaved-two.png'), 'Active unsaved draft was lost');
        assert.equal(await h.page.evaluate(() => window.comfyAPI.app.app.rootGraph.serialize().nodes.find(n => n.type === 'LoadImage').widgets_values[0]), 'unsaved-two.png');
        assert.equal(h.navigations(), before + 1);
        for (const revision of ['200', '100', '200']) {
            h.state.current = revision;
            await reconnect(h.page);
            await h.page.waitForTimeout(250);
        }
        assert.equal(h.navigations(), before + 1, 'Same/older workers caused a reload loop');
        h.state.served = h.state.current = '300';
        await h.page.reload();
        await h.page.waitForTimeout(700);
        assert.equal(h.navigations(), before + 2, 'An already updated page refreshed again');
        assert.deepEqual(h.errors, []);
        await h.context.close();
        console.log('PASS: unsaved tabs, reconnect, stale worker, once per revision, manual refresh');

        const serializers = await harness(browser);
        await openDraft(serializers.page, 'api-draft', 'api-draft.png');
        await serializers.page.evaluate(async () => {
            const { app } = await import('/scripts/app.js');
            app.graphToPrompt = async () => { throw new Error('API widget serializer failed'); };
        });
        serializers.state.served = serializers.state.current = '200';
        await Promise.all([serializers.page.waitForEvent('load'), reconnect(serializers.page)]);
        await serializers.page.waitForFunction(key => sessionStorage.getItem(key) === null && window.comfyAPI?.app?.app?.isGraphReady, backupKey);
        assert((await draftImages(serializers.page)).includes('api-draft.png'));
        await serializers.context.close();
        console.log('PASS: API prompt serializer failures do not prevent workflow backup and refresh');

        const quota = await harness(browser);
        await openDraft(quota.page, 'quota', 'keep-me.png');
        await quota.page.evaluate(key => {
            const original = Storage.prototype.setItem;
            Storage.prototype.setItem = function (name, value) {
                if (name === key) throw new DOMException('Full', 'QuotaExceededError');
                return original.call(this, name, value);
            };
        }, backupKey);
        const quotaBefore = quota.navigations();
        quota.state.served = quota.state.current = '200';
        await reconnect(quota.page);
        await quota.page.getByText('自动刷新已暂停', { exact: true }).waitFor();
        assert.equal(quota.navigations(), quotaBefore);
        assert((await draftImages(quota.page)).includes('keep-me.png'));
        await quota.context.close();
        console.log('PASS: quota failure preserves the open page');

        const auth = await harness(browser);
        const authBefore = auth.navigations();
        auth.state.status = 401;
        auth.state.current = '200';
        await reconnect(auth.page);
        await auth.page.waitForTimeout(250);
        assert.equal(auth.navigations(), authBefore);
        await auth.page.evaluate(({ backupKey, seenKey }) => {
            sessionStorage.setItem(backupKey, JSON.stringify({ user_id: 'another-user', workflows: [{ name: 'private', data: {} }] }));
            sessionStorage.setItem(seenKey, JSON.stringify({ user_id: 'another-user', revision: '999' }));
        }, { backupKey, seenKey });
        auth.state.status = 200;
        auth.state.current = '100';
        await reconnect(auth.page);
        await auth.page.waitForFunction(key => sessionStorage.getItem(key) === null, backupKey);
        assert.equal(auth.navigations(), authBefore);
        assert.deepEqual(auth.errors, []);
        await auth.context.close();
        console.log('PASS: expired login and different-user backups');

        const tabs = await harness(browser);
        const sibling = await tabs.context.newPage();
        sibling.on('pageerror', error => tabs.errors.push(error.message));
        sibling.on('dialog', async dialog => {
            tabs.errors.push('Unexpected sibling confirmation: ' + dialog.type());
            await dialog.dismiss();
        });
        let siblingNavigations = 0;
        sibling.on('request', request => {
            if (request.isNavigationRequest() && request.frame() === sibling.mainFrame()) siblingNavigations++;
        });
        await sibling.goto(baseURL);
        await sibling.waitForFunction(() => window.comfyAPI?.app?.app?.extensionManager?.workflow?.activeWorkflow && LiteGraph.registered_node_types.LoadImage && window.comfyAPI.app.app.isGraphReady);
        await openDraft(tabs.page, 'first-browser-tab', 'first-browser-tab.png');
        await openDraft(sibling, 'second-browser-tab', 'second-browser-tab.png');
        const tabsBefore = tabs.navigations();
        tabs.state.served = tabs.state.current = '200';
        await Promise.all([
            tabs.page.waitForEvent('load'), sibling.waitForEvent('load'),
            reconnect(tabs.page), reconnect(sibling),
        ]);
        for (const page of [tabs.page, sibling]) {
            await page.bringToFront();
            await page.waitForFunction(key => sessionStorage.getItem(key) === null && window.comfyAPI?.app?.app?.isGraphReady, backupKey);
        }
        assert((await draftImages(tabs.page)).includes('first-browser-tab.png'));
        assert((await draftImages(sibling)).includes('second-browser-tab.png'));
        assert.equal(tabs.navigations(), tabsBefore + 1);
        assert.equal(siblingNavigations, 2);
        assert.deepEqual(tabs.errors, []);
        await tabs.context.close();
        console.log('PASS: browser tabs refresh independently and retain their drafts');

        const recovery = await harness(browser);
        const recoveryBefore = recovery.navigations();
        await recovery.page.evaluate(key => {
            sessionStorage.setItem(key, JSON.stringify({ user_id: 'user-a', active: 0, workflows: [{ name: 'recover', data: { nodes: [] } }] }));
            window.comfyAPI.app.app.loadGraphData = async () => { throw new Error('Test recovery failure'); };
        }, backupKey);
        await reconnect(recovery.page);
        await recovery.page.getByText('自动刷新已暂停', { exact: true }).waitFor();
        assert(await recovery.page.evaluate(key => sessionStorage.getItem(key) !== null, backupKey));
        assert.equal(recovery.navigations(), recoveryBefore);
        await recovery.context.close();
        console.log('PASS: failed recovery retains the backup and pauses refresh');

        const rejected = await harness(browser);
        const rejectedBefore = rejected.navigations();
        await rejected.page.evaluate(key => {
            sessionStorage.setItem(key, JSON.stringify({ user_id: 'user-a', active: 0, workflows: [{ name: 'recover', data: { nodes: [] } }] }));
            window.comfyAPI.app.app.loadGraphData = async () => false;
        }, backupKey);
        await reconnect(rejected.page);
        await rejected.page.getByText('自动刷新已暂停', { exact: true }).waitFor();
        assert(await rejected.page.evaluate(key => sessionStorage.getItem(key) !== null, backupKey));
        assert.equal(rejected.navigations(), rejectedBefore);
        await rejected.context.close();
        console.log('PASS: rejected graph load retains the backup and pauses refresh');
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
