"""Process prompts run once per agent and completion waits for durable saves."""
import subprocess
from pathlib import Path

import pytest

from bench.study_devices import DeviceError, Devices


def test_process_request_is_idempotent_across_concurrent_calls_and_restart(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    devices = Devices(tmp_path / 'devices.db')
    agents = {'muse': 'muse'}
    devices.pair(devices.code('owner')['code'], agents, agents)
    with devices.transaction() as db:
        db.execute('update devices set verified = ?', ('["muse"]',))
    body = {'agent': 'muse', 'kind': 'run', 'step': 'TASK PROCESS 1',
            'text': 'Describe your process.', 'request_id': 'process:1:muse:opening:1'}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: devices.queue('owner', agents, 'Restaurant', body), range(8)))
    assert len({r['id'] for r in results}) == 1
    assert len(devices.status('owner')['jobs']) == 1
    devices = Devices(tmp_path / 'devices.db')
    assert devices.queue('owner', agents, 'Restaurant', body)['id'] == results[0]['id']
    with pytest.raises(DeviceError, match='already used'):
        devices.queue('owner', agents, 'Restaurant', body | {'text': 'Different prompt'})
    with pytest.raises(DeviceError, match='Open the Study Connector'):
        devices.queue('another-owner', agents, 'Restaurant', body)


def test_process_auto_sequence_reload_and_save_failure():
    source = (Path(__file__).parents[1] / 'bench/study.html').read_text()
    functions = source[source.index('  const processSending'):source.index('  function renderDone')]
    script = r'''
const assert = require('node:assert/strict');
const HOSTED=true,ONLINE=true,BROWSE=false,dryRun=false;
let saveErr='',cloudWrites=Promise.resolve();const pendingWrites=new Map();
let S={pid:'PTEST',day:1,stage:'process',connector_jobs:[]};
const d={done:{},process:{},chats:{}};
const names=['muse','instinct'];
for(const a of names)d.chats[a]={sent:[{job:'opening-'+a,step:'TASK OPENING'}],manual:[]};
const D=()=>d,order=()=>names,usesConnector=()=>true,chatOf=a=>d.chats[a];
const connection={device:{online:true,verified:names}};
const jobs=[],calls=[],replies=[];
const jobById=id=>jobs.find(j=>j.id===id);
const stepName=(kind,n)=>'TASK '+kind+' '+n;
const spec={messages:{process:['First question','Second question']}};
const transcript=a=>replies.filter(r=>r.agent===a);
const elements=new Map();
const $=id=>{if(!elements.has(id))elements.set(id,{dataset:{},querySelectorAll:()=>[]});return elements.get(id);};
const esc=s=>String(s),labelOf=a=>a,sendLabel=s=>s;
const saveProgress=async()=>{};
let failSave=true,exports=0;
const saveConversation=async()=>{exports++;if(failSave)saveErr='Simulated save failure';};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const sendToAgent=async(a,text,step,extra)=>{
 calls.push([a,step,extra.request_id]);await tick();
 const e={job:a+'-'+step,text,step,...extra};chatOf(a).sent.push(e);
 jobs.push({id:e.job,state:'running'});return e;
};
''' + functions + r'''
(async()=>{
 updateProcess();updateProcess();updateProcess();
 assert.equal(calls.length,2);
 await tick();await tick();
 // A streamed first reply is not yet complete; the other agent remains independent.
 replies.push({agent:'muse',role:'agent',step:'TASK PROCESS 1',text:'First reply',at:'2026-11-17T06:00:00Z'});
 updateProcess();assert.equal(calls.length,2);
 jobs.find(j=>j.id==='muse-TASK PROCESS 1').state='done';
 updateProcess();updateProcess();assert.equal(calls.length,3);
 await tick();await tick();
 assert.equal(calls[2][0],'muse');assert.equal(calls[2][1],'TASK PROCESS 2');
 // A failed first job must not cause automatic retries or a second prompt.
 jobs.find(j=>j.id==='instinct-TASK PROCESS 1').state='failed';
 updateProcess();updateProcess();assert.equal(calls.length,3);
 // Simulate the user supplying a complete first reply through the fallback.
 replies.push({agent:'instinct',role:'agent',step:'TASK PROCESS 1',text:'Complete manual reply',at:'2026-11-17T06:00:01Z'});
 d.process.instinct.accepted={1:true};updateProcess();
 await tick();await tick();assert.equal(calls.length,4);
 for(const a of names){
  replies.push({agent:a,role:'agent',step:'TASK PROCESS 2',text:'Second reply',at:'2026-11-17T06:00:02Z'});
 }
 updateProcess();assert.equal($('p-done').disabled,true);
 for(const j of jobs.filter(j=>j.id.endsWith('PROCESS 2')))j.state='done';
 updateProcess();await tick();await tick();
 assert.equal($('p-done').disabled,true);
 assert.match($('p-err').textContent,/save failure/);
 assert.equal($('p-save-retry').hidden,false);
 failSave=false;saveErr='';processSaveError='';updateProcess();
 await tick();await tick();
 assert.equal($('p-done').disabled,false);assert.equal(exports,4);
 // Reloaded saved entries/replies never start the prompts again.
 S=JSON.parse(JSON.stringify(S));processSending.clear();updateProcess();
 assert.equal(calls.length,4);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
