const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness() {
    const events = new Map(), storage = new Map(), warnings = [], timers = new Map();
    let extension, reloads = 0;
    const api = {
        addEventListener: (name, fn) => events.set(name, fn),
        fetchApi: async () => ({ ok: true, json: async () => ({ revision: '200', user_id: 'a' }) }),
    };
    const app = {
        isGraphReady: true,
        graphToPrompt: async () => ({ workflow: { nodes: [] } }),
        extensionManager: {
            workflow: { openWorkflows: [] },
            toast: { add: item => warnings.push(item) },
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
    return { app, api, warnings, storage, sessionStorage, events, reloads: () => reloads };
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
