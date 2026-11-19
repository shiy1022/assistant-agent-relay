"""Replies survive loss of the connector's temporary job queue."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_hosted_chat_survives_reload_and_job_expiry():
    if not shutil.which('node'):
        pytest.skip('Node is required for the browser logic regression test')
    source = (Path(__file__).parents[1] / 'bench/study.html').read_text()
    functions = source[source.index('  function retainConnectorMessages()'):source.index('  // The conversation as it happened:')]
    script = '''
const assert = require('node:assert/strict');
const HOSTED = true, connectorEnabled = true;
let saves = 0;
let S = {d: {'1': {chats: {instinct: {sent: [{job:'job1',step:'OPENING',round:0}],manual:[]}}}}};
let jobs = [{id:'job1',sent_at:'2026-11-15T12:00:00Z',replies:[{id:'reply1',text:'pong',at:'2026-11-15T12:00:01Z'}]}];
const jobById = id => jobs.find(j => j.id === id);
const chatOf = agent => S.d['1'].chats[agent];
const saveProgress = () => saves++;
''' + functions + '''
retainConnectorMessages();
assert.equal(saves,1);
retainConnectorMessages();
assert.equal(saves,1);
jobs[0].replies[0].text = 'pong updated';
retainConnectorMessages();
assert.equal(saves,2);
S = JSON.parse(JSON.stringify(S));
jobs = [];
assert.equal(agentMessages('instinct',null)[0].text,'pong updated');
assert.equal(chatOf('instinct').sent[0].sent_at,'2026-11-15T12:00:00Z');
assert.equal(agentMessages('instinct',null).length,1);
jobs = [{id:'hark-test',agent:'hark',kind:'test',prompt:'reply pong',state:'done',replies:[{id:'hark-reply',text:'pong'}]}];
retainConnectorMessages();
assert.equal(S.connector_jobs.find(j=>j.agent==='hark').replies[0].text,'pong');
S = JSON.parse(JSON.stringify(S));
jobs=[];
retainConnectorMessages();
assert.equal(S.connector_jobs.find(j=>j.agent==='hark').replies[0].text,'pong');

'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
