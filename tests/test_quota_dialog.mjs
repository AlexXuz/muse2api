import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(new URL('../engine.py', import.meta.url), 'utf8');
const script = source.match(/_QUOTA_DIALOG_JS = r"""([\s\S]*?)"""/)[1];
const element = (text, hidden=false) => ({innerText:text,hidden,getClientRects:()=>hidden?[]:[{}]});
function read(dialogs){return vm.runInNewContext(script,{
  document:{querySelectorAll:()=>dialogs},getComputedStyle:e=>({visibility:e.hidden?'hidden':'visible'})
});}
const quota = 'Settings\nUsage\nFree plan\n17% used';
assert.equal(read([element('Notice\n50% used'),element(quota)]),quota);
assert.equal(read([element('Usage\n99% used',true),element(quota)]),quota);
assert.equal(read([element('Unrelated\n50% used')]),'');
assert.equal(read([element('Settings\nGeneral')]),'Settings\nGeneral');
assert.equal(read([element('设置\n用量\n已用 17%')]),'设置\n用量\n已用 17%');
console.log('PASS: 5 Settings/usage dialog selection checks');
