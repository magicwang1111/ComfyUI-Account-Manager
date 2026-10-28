const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness() {
    const events = new Map(), storage = new Map(), warnings = [], timers = new Map(), toasts = [];
    let extension, reloads = 0;
    const api = {
        addEventListener: (name, fn) => events.set(name, fn),
        fetchApi: async () => ({ ok: true, json: async () => ({ revision: '200', user_id: 'a' }) }),
    };
    const app = {
        isGraphReady: true,
        ui: { settings: { settingsValues: {} } },
        graphToPrompt: async () => ({ workflow: { nodes: [] } }),
        extensionManager: {
            workflow: { openWorkflows: [] },
            toast: {
                add: item => { toasts.push(item); if (item.severity === 'warn') warnings.push(item); },
                remove: item => { const i = toasts.indexOf(item); if (i >= 0) toasts.splice(i, 1); },
            },
            setting: { settings: {} },
        },
        registerExtension: value => { extension = value; },
    };
    const sessionStorage = {
        getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
        removeItem: key => storage.delete(key),
    };
    const source = fs.readFileSync(require.resolve('../web/js/autoRefresh.js'), 'utf8')
        .replace(/^import .*;\r?\n/gm, '')
        .replace('import.meta.url', JSON.stringify('http://localhost/autoRefresh.js?am_revision=100'));
    vm.runInNewContext(source, {
        app, api, sessionStorage, URL, AbortSignal, console: { error() {} },
        setTimeout: fn => { const id = {}; timers.set(id, fn); return id; },
        clearTimeout: id => timers.delete(id),
        window: { addEventListener() {}, location: { reload: () => reloads++ } },
        document: { addEventListener() {} },
    });
    extension.setup();
    return { app, api, warnings, toasts, storage, sessionStorage, events, reloads: () => reloads };
}

test('server unavailable does not warn or reload', async () => {
    const h = harness();
    h.api.fetchApi = async () => { throw new TypeError('Failed to fetch'); };
    await h.events.get('reconnected')();
    assert.equal(h.warnings.length, 0);
    assert.equal(h.reloads(), 0);
});

for (const fail of [true, false]) {
    test(`disconnect during snapshot (${fail ? 'failure' : 'success'}) waits for reconnect`, async () => {
        const h = harness();
        h.app.graphToPrompt = async () => {
            h.events.get('reconnecting')?.();
            if (fail) throw new TypeError('Failed to fetch');
            return { workflow: { nodes: [] } };
        };
        await h.events.get('reconnected')();
        assert.equal(h.warnings.length, 0);
        assert.equal(h.reloads(), 0);
        assert.equal(h.storage.has('AccountManager.RefreshRevision'), false);
        h.app.graphToPrompt = async () => ({ workflow: { nodes: [] } });
        await h.events.get('reconnected')();
        assert.equal(h.reloads(), 1);
    });
}

test('real local storage failure still pauses refresh', async () => {
    const h = harness();
    h.sessionStorage.setItem = () => { throw new Error('Quota exceeded'); };
    await h.events.get('reconnected')();
    await h.events.get('reconnected')();
    assert.equal(h.warnings.length, 1);
    assert.equal(h.reloads(), 0);
});

test('disconnect while version response is pending discards the response', async () => {
    const h = harness();
    const fetch = h.api.fetchApi;
    h.api.fetchApi = async () => {
        h.events.get('reconnecting')();
        return fetch();
    };
    await h.events.get('reconnected')();
    assert.equal(h.reloads(), 0);
    assert.equal(h.warnings.length, 0);
    h.api.fetchApi = fetch;
    await h.events.get('reconnected')();
    assert.equal(h.reloads(), 1);
});

test('disconnect shows one waiting notification and reconnect removes it', async () => {
    const h = harness();
    h.api.fetchApi = async () => ({ ok: true, json: async () => ({ revision: '100', user_id: 'a' }) });
    h.events.get('reconnecting')();
    h.events.get('reconnecting')();
    assert.equal(h.toasts.length, 1);
    assert.equal(h.toasts[0].summary, '服务器连接已断开，正在等待恢复');
    assert.equal(h.app.ui.settings.settingsValues['Comfy.Toast.DisableReconnectingToast'], true);
    assert.equal(h.app.extensionManager.setting.settings['Comfy.Toast.DisableReconnectingToast'], undefined);
    await h.events.get('reconnected')();
    assert.equal(h.toasts.length, 0);
    h.events.get('reconnecting')();
    assert.equal(h.toasts.length, 1);
});
