"""Public download integrity and Muse-only onboarding behavior."""
import json
import subprocess
import threading
from pathlib import Path
from urllib.request import urlopen

from bench.hosted_study import StudyServer

ROOT = Path(__file__).parents[1]


def test_signed_shortcut_download_is_public_and_preserves_bytes(tmp_path, monkeypatch):
    spec = tmp_path / 'study.json'
    spec.write_text(json.dumps({'task': 'Synthetic', 'pre': []}))
    monkeypatch.setenv('STUDY_SPEC_FILE', str(spec))
    server = StudyServer('http://127.0.0.1', ('127.0.0.1', 0), object(), tmp_path, 'Synthetic', [])
    server.host = f'127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f'http://{server.host}/study-setup.shortcut') as response:
            assert response.status == 200
            assert response.headers['Content-Disposition'] == 'attachment; filename="Study Connect.shortcut"'
            assert response.read() == (ROOT / 'bench/Connect Study - Muse.shortcut').read_bytes()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_shared_shortcut_keeps_pairing_code_visible_for_supported_channels():
    source = (ROOT / 'bench/study_connect.js').read_text()
    update = source[source.index('  function updateView()'):source.index('  function renderMessages()')]
    script = '''
const assert=require('node:assert/strict');
const elements=new Map();
const el=id=>{if(!elements.has(id)) elements.set(id,{});return elements.get(id);};
let mounted=true,latest={device:null,jobs:[]},selected='muse',lastError='',actionPending=false;
let codeExpiry=Date.now()+300000,shortcutSupported=true,lastShortcutMode=null;
const picker={selectedOptions:[{dataset:{channel:'muse'}}]};
const dialog={querySelector:el};
const status={},setup={dataset:{}},test={},disconnect={},direction={},codeBox={},codeText={textContent:'test-code'};
const imessagePages={};
const labels={muse:'Muse',whatsapp:'WhatsApp'};const taskNavigation={};const help={muse:'Sign in to Muse'};const startCommand='run';
const renderMessages=()=>{};
''' + update + '''
codeExpiry=0;updateView();
assert.equal(el('#connector-run-shortcut').hidden,false);
assert.equal(codeBox.hidden,true);
codeExpiry=Date.now()+300000;updateView();
assert.equal(el('#connector-shortcut').hidden,false);
assert.equal(el('#connector-install-manual').open,false);
assert.equal(el('#connector-terminal').open,false);
assert.equal(codeBox.hidden,false);
assert.equal(el('#connector-run-shortcut').hidden,false);
assert.match(el('#connector-code-help').textContent,/automatically/);
// Polling must not collapse a fallback the participant has explicitly opened.
el('#connector-install-manual').open=true;
updateView();assert.equal(el('#connector-install-manual').open,true);
picker.selectedOptions[0].dataset.channel='whatsapp';selected='whatsapp';updateView();
assert.equal(el('#connector-shortcut').hidden,false);
assert.equal(el('#connector-run-shortcut').hidden,false);
assert.equal(el('#connector-install-manual').open,true);
assert.equal(el('#connector-terminal').open,false);
assert.match(el('#connector-code-help').textContent,/automatically/);
codeExpiry=Date.now()-1;updateView();assert.equal(codeBox.hidden,true);
latest.device={online:true,agents:{whatsapp:'whatsapp'},verified:[]};updateView();
assert.equal(el('#connector-install').hidden,true);
assert.equal(test.disabled,false);
// iMessage shows one step at a time and never offers a test before permission setup.
picker.selectedOptions[0].dataset.channel='imessage';selected='instinct';
latest.device.agents.instinct='imessage';updateView();
assert.equal(setup.dataset.setupStep,'contact');
assert.equal(test.hidden,true);
assert.equal(el('#connector-imessage-reader').hidden,true);
latest.jobs=[{agent:'instinct',kind:'setup',step:'IMESSAGE CONTACT',state:'done'}];updateView();
assert.equal(setup.dataset.setupStep,'signin');
assert.equal(el('#connector-imessage-next').hidden,false);
assert.equal(el('#connector-imessage-reader').hidden,true);
imessagePages.instinct='permissions';updateView();
assert.equal(setup.dataset.setupStep,'permissions');
assert.equal(test.hidden,true);
delete imessagePages.instinct;
latest.jobs.push({agent:'instinct',kind:'setup',step:'IMESSAGE PERMISSIONS',state:'done'});updateView();
assert.equal(test.hidden,false);
assert.equal(el('#connector-imessage-reader').hidden,false);
assert.equal(el('#connector-imessage-next').hidden,true);
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)


def test_copy_creates_agent_bound_code_once_and_handles_clipboard_denial():
    source = (ROOT / 'bench/study_connect.js').read_text()
    method = source[source.index('  async function copyPairingCode()'):source.index('  function copyButton(')]
    script = '''
const assert=require('node:assert/strict');
let codeExpiry=0,selected='muse',autoSetupAgent='muse';
const codeText={textContent:''},button={};
const dialog={querySelector:()=>button};
let scrolls=0,calls=[],copied=[];
const codeBox={hidden:false,scrollIntoView:()=>scrolls++};
const url='/api/study/connection';
const action=fn=>fn();
const studyAuth={api:async(path,body)=>{calls.push([path,body]);return {code:'synthetic-code',expires_in:300};}};
const navigator={clipboard:{writeText:async text=>copied.push(text)}};
''' + method + '''
(async()=>{
 await copyPairingCode();
 assert.deepEqual(calls,[['/api/study/connection/code',{agent:'muse'}]]);
 assert.deepEqual(copied,['synthetic-code']);
 assert.equal(autoSetupAgent,null);
 await copyPairingCode();assert.equal(calls.length,1);
 codeExpiry=0;
 navigator.clipboard.writeText=async()=>{throw Error('denied');};
 await copyPairingCode();assert.equal(calls.length,2);
 assert.equal(codeText.textContent,'synthetic-code');
 assert.match(button.textContent,/Select the code/);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
