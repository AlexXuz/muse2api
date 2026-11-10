/** Actual admin quota functions, with only the API/DOM edges mocked. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('../admin.html', import.meta.url), 'utf8');
const start = html.indexOf('/* ================= 额度显示 ================= */');
const end = html.indexOf('/* ================= 有效期显示 ================= */', start);
const source = html.slice(start, end);
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const esc = s => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function harness(){
  const calls=[], notices=[], responses=[];let loads=0;
  const ctx=vm.createContext({esc,fmtTime:t=>'at '+t,ago:t=>'since '+t,
    toast:(...a)=>notices.push(a),loadPool:()=>{loads++;},
    api:async(path,opts)=>{calls.push({path,opts});let r=responses.shift();if(typeof r==='function')r=await r();if(r instanceof Error)throw r;return r;}});
  vm.runInContext(source,ctx);
  return {ctx,calls,notices,responses,loads:()=>loads,run:s=>vm.runInContext(s,ctx)};
}
let checks=0;
async function check(name,fn){await fn();console.log('PASS:',name);checks++;}

await check('duplicate calls and freshly rendered controls share the account pending state',async()=>{
  const h=harness();let release;h.responses.push(()=>new Promise(r=>{release=r;}));
  const oldButton={};const pending=h.ctx.queryQuota('account-a',oldButton);
  assert.equal(oldButton.disabled,true);
  assert.match(h.ctx.quotaBadge({id:'account-a'}),/正在读取官网额度/);
  await h.ctx.queryQuota('account-a',{});assert.equal(h.calls.length,1);
  assert.match(html,/queryQuota\('\$\{a.id\}', this\)" \$\{quotaPending.has\(a.id\) \? 'disabled'/);
  release({found:true,weekly_used_pct:0,plan:'Free plan'});await pending;
  assert.equal(h.run('quotaPending.size'),0);assert.equal(oldButton.disabled,false);
  assert.ok(h.loads()>=2);
});
await check('failed refresh restores controls and refreshes the persistent cache notice',async()=>{
  const h=harness(),button={};h.responses.push(Error('usage not ready'));
  await h.ctx.queryQuota('account-a',button);
  assert.equal(button.disabled,false);assert.equal(button.textContent,'额度');
  assert.equal(h.run('quotaPending.size'),0);
  assert.match(h.notices.at(-1)[0],/usage not ready/);
  assert.equal(h.loads(),2);
});
await check('cached success is explicitly marked after a newer failed attempt',async()=>{
  const h=harness();const markup=h.ctx.quotaBadge({id:'a',quota:{found:true,plan:'Free plan',weekly_used_pct:0,checked_at:100},quota_attempted_at:110,quota_error:'delayed <script>'});
  assert.match(markup,/缓存 · Free · 已用 0%/);assert.match(markup,/上次成功 since 100/);
  assert.match(markup,/刷新失败，显示上次成功结果/);assert.ok(!markup.includes('<script>'));
});
await check('old errors do not label a more recent successful cache as a failed refresh',async()=>{
  const h=harness();const markup=h.ctx.quotaBadge({quota:{found:true,weekly_used_pct:1,checked_at:200},quota_attempted_at:110,quota_error:'old'});
  assert.ok(!markup.includes('缓存 ·'));assert.ok(!markup.includes('刷新失败'));
  assert.match(markup,/上次成功 since 200/);
});
await check('no cached success and an error displays read failure rather than zero balance',async()=>{
  const h=harness();const markup=h.ctx.quotaBadge({quota_error:'failed'});
  assert.match(markup,/读取失败，请重试/);assert.ok(!markup.includes('0%'));
});
await check('different accounts remain independent in the UI',async()=>{
  const h=harness();let release;
  h.responses.push(()=>new Promise(r=>{release=r;}),{found:true,weekly_used_pct:2});
  const first=h.ctx.queryQuota('a',{});await h.ctx.queryQuota('b',{});
  assert.equal(h.calls.length,2);assert.equal(h.run('quotaPending.has("a")'),true);
  assert.equal(h.run('quotaPending.has("b")'),false);
  release({found:true,weekly_used_pct:1});await first;
});
console.log(`PASS: ${checks} quota UI cases; no network or account`);
