"""Automatic web login opens once after pairing and waits for active work."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_auto_setup_respects_pairing_dialog_and_busy_jobs():
    if not shutil.which('node'):
        pytest.skip('Node required for browser logic test')
    source = (Path(__file__).parents[1] / 'bench/study_connect.js').read_text()
    functions = source[source.index('  function requestWebSetup()'):source.index('  async function refresh()')]
    script = '''
const assert = require('node:assert/strict');
let autoSetupAgent = null, selected = 'hark', actionPending = false;
const labels = {hark:'Hark',muse:'Muse',wajo:'Wajo',imessage:'iMessage'};
const dialog = {open:true};
const picker = {selectedOptions:[{dataset:{channel:'hark'}}]};
const pairLink = {closest:()=>dialog};
let latest = {device:null,jobs:[]};
const calls=[];
const queue=(kind,agent)=>calls.push([kind,agent]);
const action=fn=>fn();
''' + functions + '''
requestWebSetup();
assert.equal(calls.length,0);
latest.device={online:true,verified:[]};
latest.jobs=[{state:'running'}];
maybeOpenWebSetup();
assert.equal(calls.length,0);
latest.jobs=[];
maybeOpenWebSetup();
maybeOpenWebSetup();
assert.deepEqual(calls,[['setup','hark']]);
dialog.open=false;
requestWebSetup();
assert.equal(calls.length,1);
dialog.open=true;
selected='instinct';
picker.selectedOptions[0].dataset.channel='imessage';
requestWebSetup();
assert.equal(autoSetupAgent,null);
assert.equal(calls.length,1); // iMessage opens only after an explicit step button.
selected='muse';
picker.selectedOptions[0].dataset.channel='muse';
latest.device.verified=['muse'];
requestWebSetup();
assert.equal(calls.length,2);
assert.deepEqual(calls[1],['setup','muse']);
latest.jobs=[{agent:'muse',kind:'setup',state:'queued'}];
requestWebSetup();assert.equal(autoSetupAgent,null);
latest.jobs=[];maybeOpenWebSetup();assert.equal(calls.length,2);
'''
    subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
