"""Protocol plumbing tests use synthetic questions, never the private task bank."""
import json
import subprocess
from pathlib import Path

from bench.hosted_study import ScopedResults, StudyServer
from bench.study_devices import Devices


def test_task_scope_keeps_legacy_and_new_results_separate():
    class DB:
        calls = []
        def save(self, *args): self.calls.append(args)
        def rows(self, table, token, **filters):
            self.calls.append(filters)
            return iter([{'path':'tasks/example-v1/progress.json','content':{'stage':'setup'}}])
    db = DB()
    scoped = ScopedResults(db, 'example-v1')
    scoped.save('token','P001','progress.json',{'stage':'setup'})
    assert db.calls[-1][2] == 'tasks/example-v1/progress.json'
    assert list(scoped.rows('study_results','token',path='in.(progress.json,session.json)'))[0]['path'] == 'progress.json'
    assert db.calls[-1]['path'] == 'in.(tasks/example-v1/progress.json,tasks/example-v1/session.json)'
    list(scoped.rows('study_result_history','token',pid='eq.P001'))
    assert db.calls[-1]['path'] == 'like.tasks/example-v1/*'


def test_secret_spec_loads_without_public_task_files(monkeypatch, tmp_path):
    spec = tmp_path / 'private.json'
    spec.write_text(json.dumps({'task':'Synthetic v1','pre':[], 'study_days':1}))
    monkeypatch.setenv('STUDY_SPEC_FILE',str(spec))
    server = StudyServer('http://127.0.0.1:8874',('127.0.0.1',0),object(),tmp_path,'Synthetic v1',[])
    try: assert server.spec['study_days'] == 1
    finally: server.server_close()


def test_other_agent_runs_without_reentering_busy_agent(tmp_path):
    d = Devices(tmp_path/'jobs.db')
    agents={'alpha':'hark','beta':'muse'}
    token=d.pair(d.code('user')['code'],agents,agents)['credential']
    for agent in agents:
        d.queue('user',agents,'Synthetic',{'kind':'test','agent':agent})
        job=d.exchange(token,'poll',{'agents':agents},agents)['job']
        reply=job['prompt'].rsplit(': ',1)[-1]
        d.exchange(token,'report',{'id':job['id'],'state':'done','sent_at':'2026-11-16T12:00:00Z',
            'replies':[{'id':agent,'at':'2026-11-16T12:00:01Z','text':reply}]},agents)
    for agent in ['alpha','alpha','beta']:
        d.queue('user',agents,'Synthetic',{'kind':'run','agent':agent,'text':'Synthetic opening','step':'TASK OPENING'})
    assert d.exchange(token,'poll',{'agents':agents},agents)['job']['agent']=='alpha'
    assert d.exchange(token,'poll',{'agents':agents,'busy_agents':['alpha']},agents)['job']['agent']=='beta'
    assert d.exchange(token,'poll',{'agents':agents,'busy_agents':['alpha','beta']},agents)['job'] is None


def test_partial_parallel_enqueue_retains_success_and_failed_retry():
    source=(Path(__file__).parents[1]/'bench/study.html').read_text()
    method=source[source.index('  async function sendTogether('):source.index('  function updateTogether()')]
    script='''
const assert=require('node:assert/strict');
const chats={alpha:{sent:[]},beta:{sent:[]}};
const chatOf=a=>chats[a];const nowIso=()=> 'now';let saves=0;
const saveProgress=()=>saves++;
async function sendToAgent(agent,text,step,extra){
 if(agent==='beta') throw new Error('offline');
 const entry={job:'accepted',text,step,...extra};chats[agent].sent.push(entry);return entry;
}
'''+method+'''
(async()=>{
 const out=await sendTogether(['alpha','beta'],()=> 'opening',()=> 'TASK OPENING',()=>({round:0}));
 assert.equal(out.length,2);
 assert.equal(chats.alpha.sent.length,1);
 assert.equal(chats.alpha.sent[0].job,'accepted');
 assert.equal(chats.beta.sent.length,1);
 assert.equal(chats.beta.sent[0].enqueue_error,'offline');
 assert.equal(saves,1);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)


def test_parallel_stage_order_and_round_offset():
    source=(Path(__file__).parents[1]/'bench/study.html').read_text()
    stages=source[source.index('  const TOGETHER ='):source.index('  const isDone =')]
    names=source[source.index('  const ROUND_OFF ='):source.index('  function effectiveCard()')]
    script='''
const assert=require('node:assert/strict');
const S={day:1,session:{agents:['alpha','beta'],day1_order:['alpha','beta']}};
const spec={chat_mode:'together',study_days:1,pre:[{id:'pick',title:'Choose'},{id:'lock',title:'Lock'}],
 round_offset:1,step_prefix:'TASK',limits:{triggers:{turns:'five_rounds',minute:'minute_15',ready:'participant_ready'}},words:{tie:'No difference'}};
const order=()=>S.session.day1_order;
const labelOf=a=>a==='alpha'?'A':'B';
'''+names+stages+'''
assert.deepEqual(stages().map(s=>s.key),['setup','pre:pick','pre:lock','chat:0','after:1','after:2','compare','day1','process','done']);
assert.equal(stepName('ROUND',4),'TASK ROUND 5');
assert.equal(stepName('OPENING'),'TASK OPENING');
assert.equal(TRIG('ready'),'participant_ready');
assert.equal(TIE(),'No difference');
'''
    subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
