const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const fixture = spawnSync(process.env.COMFY_TEST_PYTHON || 'python', ['-c', `
import importlib.util,json,sys,tempfile
from pathlib import Path
root=Path.cwd()
sys.path.insert(0,str(root.parents[1]))
from comfy_execution.jobs import get_all_jobs,normalize_history_item
spec=importlib.util.spec_from_file_location('history_store',root/'utils/history_store.py')
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
with tempfile.TemporaryDirectory() as directory:
 store=module.HistoryStore(str(Path(directory)/'history.db'))
 old={'prompt':[0,'recovered',{}, {'create_time':1000},[]], 'user_id':'a',
      'api_recovery':{'task':{'state':'recovered'}},
      'outputs':{'api_recovery_task':{'video':[{'filename':'recovered-test.mp4','subfolder':'a/recovered','type':'output','format':'video/mp4'}]}},
      'status':{'status_str':'error','completed':False,'messages':[
        ['execution_start',{'timestamp':1000}],
        ['execution_error',{'node_id':'7','node_type':'APIRecovery',
          'exception_type':'WorkerInterrupted','exception_message':'Interrupted','timestamp':1100}]]}}
 store.save('recovered',old,10)
 store.save('previous',{'prompt':[1,'previous',{}, {'create_time':900},[]],
   'user_id':'a','outputs':{},'status':{'status_str':'success','completed':True,'messages':[]}},10)
 history=store.query(owner_id='a',for_jobs=True)
 jobs,total=get_all_jobs([],[],history,limit=200,offset=0)
 print(json.dumps({'page':{'jobs':jobs,'pagination':{'offset':0,'limit':200,'total':total,'has_more':False}},
   'detail':normalize_history_item('recovered',store.query(prompt_id='recovered')['recovered'],include_outputs=True)}))
`], { cwd: path.resolve(__dirname, '..'), encoding: 'utf8' });
assert.equal(fixture.status, 0, fixture.stderr);
const payload = JSON.parse(fixture.stdout);

(async () => {
    const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined });
    try {
        const page = await browser.newPage();
        await page.route('**/extensions', async route => {
            const response = await route.fetch();
            const extensions = await response.json();
            extensions.push('/extensions/ComfyUI-Account-Manager/js/apiRecovery.js');
            await route.fulfill({ json: extensions });
        });
        await page.route('**/extensions/ComfyUI-Account-Manager/js/apiRecovery.js', route => route.fulfill({
            contentType: 'application/javascript', body: fs.readFileSync(path.join(__dirname, '../web/js/apiRecovery.js'), 'utf8'),
        }));
        let broken = true;
        await page.route('**/api/jobs?*', route => {
            const body = structuredClone(payload.page);
            if (broken) delete body.jobs.find(job => job.id === 'recovered').execution_error.traceback;
            return route.fulfill({ json: body });
        });
        await page.route('**/api/jobs/recovered', route => route.fulfill({ json: payload.detail }));
        await page.goto(process.env.COMFY_TEST_URL || 'http://127.0.0.1:8200');
        await page.waitForFunction(() => window.comfyAPI?.api?.api);
        const read = () => page.evaluate(async () => {
            const { api } = await import('/scripts/api.js');
            return (await api.getHistory()).map(job => job.id);
        });
        assert.deepEqual(await read(), [], 'Missing traceback should reproduce the empty history page');
        broken = false;
        assert.deepEqual((await read()).sort(), ['previous', 'recovered']);
        const detail = await page.evaluate(async () => {
            const { api } = await import('/scripts/api.js');
            return api.getJobDetail('recovered');
        });
        assert.equal(detail.id, 'recovered');
        assert.deepEqual(detail.execution_error.traceback, []);
        await page.reload();
        await page.waitForFunction(() => window.comfyAPI?.app?.app?.extensionManager?.workflow?.activeWorkflow);
        await page.locator('#splash-loader').waitFor({ state: 'hidden' });
        if (!await page.getByTestId('job-assets-list').isVisible()) {
            await page.getByTestId('queue-overlay-toggle').click();
        }
        const button = page.locator('[data-job-id="recovered"]').getByRole('button', { name: '查看已恢复视频' });
        await button.waitFor();
        await button.click();
        const video = page.locator('.am-recovery-dialog video');
        await video.waitFor();
        assert((await video.getAttribute('src')).includes('filename=recovered-test.mp4'));
        await page.locator('.am-recovery-dialog').getByRole('button', { name: '关闭' }).click();
        await video.waitFor({ state: 'detached' });
        console.log('PASS: real frontend reproduces empty history and accepts repaired legacy recovery list/detail');
        console.log('PASS: interrupted job retains failed status and opens recovered media from its queue card');
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
