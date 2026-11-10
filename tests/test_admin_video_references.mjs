/** Offline behavior checks of the actual video uploader and task polling code. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('../admin.html', import.meta.url), 'utf8');
const start = html.indexOf('/* ================= 视频参考图片 ================= */');
const end = html.indexOf('/* ================= 视频参考图片结束 ================= */', start);
assert.ok(start >= 0 && end > start);
const source = html.slice(start, end);
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1], {filename:'admin-inline.js'});
assert.match(html, /id="vidRefInput"[^>]*accept="image\/png,image\/jpeg,image\/webp"[^>]*multiple/);
assert.doesNotMatch(source, /localStorage|sessionStorage/);

const file = (name, overrides={}) => ({name, type:'image/png', size:100,
  data:'data:image/png;base64,'+Buffer.from(name).toString('base64'), ...overrides});
const completed = {id:'task-1',status:'completed',result:{url:'https://example.test/video.mp4',bytes:123}};

function harness(){
  const elements = new Map(), calls = [], responses = [], messages = [];
  const created = [], revoked = [], readers = [], events = new Map(), timers = new Map();
  let now=1_800_000_000_000, nextTimer=0, mediaLoads=0, taskLoads=0;
  const $ = id => {
    if (!elements.has(id)) elements.set(id,{value:'',disabled:false,innerHTML:'',textContent:'',className:''});
    return elements.get(id);
  };
  $('vidPrompt').value='A cat waves';$('vidDur').value='5';$('vidSize').value='16:9';
  const context = {
    $, AbortController,
    Date:class extends Date {static now(){return now;}},
    URL:{createObjectURL:f=>{const url='blob:fixture-'+created.length;created.push({url,file:f});return url;},
      revokeObjectURL:url=>revoked.push(url)},
    FileReader:class {
      readAsDataURL(f){
        if(f.throwRead) throw Error('fixture read exception');
        const finish=()=>{
          if(f.fail) this.onerror();
          else {this.result=f.data;this.onload();}
        };
        if(f.hold) readers.push({file:f,finish}); else queueMicrotask(finish);
      }
      abort(){this.onabort?.();}
    },
    window:{addEventListener:(name,cb)=>events.set(name,cb)},
    setTimeout:(callback,delay)=>{
      const id=++nextTimer;timers.set(id,{callback,delay});
      // Advance only polling sleeps automatically; request/read deadlines stay pending.
      if(delay<=4000)queueMicrotask(()=>{if(timers.has(id)){timers.delete(id);now+=delay;callback();}});
      return id;
    },
    clearTimeout:id=>timers.delete(id),
    esc:text=>String(text).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])),
    fmtBytes:n=>n+' B',toast:(...args)=>messages.push(args),
    loadMedia:()=>{mediaLoads++;},loadTasks:()=>{taskLoads++;},
    api:async(path,opts)=>{
      calls.push({path,opts});
      assert.ok(responses.length,'unexpected request: '+path);
      const response=responses.shift();
      if(response instanceof Error)throw response;
      return typeof response==='function'?response(path,opts):response;
    },
  };
  vm.createContext(context);vm.runInContext(source,context);
  return {context,$,calls,responses,messages,created,revoked,readers,events,timers,
    run:code=>vm.runInContext(code,context),advance:ms=>{now+=ms;},
    refreshCounts:()=>[mediaLoads,taskLoads],
    finish:()=>responses.push({id:'task-1',status:'queued'},completed),
  };
}

let checks=0;
async function check(name,fn){await fn();checks++;console.log('PASS',name);}
function restored(h){
  for(const id of ['vidBtn','vidPrompt','vidDur','vidSize','vidRefInput'])assert.equal(h.$(id).disabled,false,id);
  assert.equal(h.run('videoBusy'),false);assert.equal(h.$('vidBtn').textContent,'开始生成视频');
}
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};

await check('text-only generation omits images and keeps player and refreshes',async()=>{
  const h=harness();h.finish();await h.context.runVideo();
  assert.deepEqual(JSON.parse(h.calls[0].opts.body),{prompt:'A cat waves',duration:5,size:'16:9'});
  assert.equal(h.calls[0].path,'/v1/videos');assert.equal(h.calls[1].path,'/v1/videos/task-1');
  assert.match(h.$('vidRes').innerHTML,/<video .* controls playsinline>/);assert.deepEqual(h.refreshCounts(),[1,1]);restored(h);
});

await check('multiple files retain selection order despite reversed read completion',async()=>{
  const h=harness();const files=[file('one.png',{hold:true}),file('two.jpg',{type:'image/jpeg',data:'data:image/jpeg;base64,Yg==',hold:true}),file('three.webp',{type:'image/webp',data:'data:image/webp;base64,Yw==',hold:true})];
  h.context.addVideoReferences(files);h.$('vidPrompt').value='[图1] moves toward [图2]';h.finish();
  const pending=h.context.runVideo();
  assert.equal(h.$('vidRefInput').disabled,true);assert.equal(h.$('vidRefClear').disabled,true);
  assert.match(h.$('vidRefPreview').innerHTML,/aria-label="移除图1" disabled/);
  h.$('vidPrompt').value='changed after submission';
  h.context.removeVideoReference(0);h.context.clearVideoReferences();h.context.addVideoReferences([file('late.png')]);
  assert.equal(h.run('videoReferences.length'),3);
  for(const i of [2,0,1])h.readers[i].finish();await pending;
  const payload=JSON.parse(h.calls[0].opts.body);
  assert.equal(payload.prompt,'[图1] moves toward [图2]');assert.deepEqual(payload.images,files.map(f=>f.data));restored(h);
});

await check('remove and clear revoke previews, renumber, and safely display filenames',async()=>{
  const h=harness();h.context.addVideoReferences([file('<bad>.png'),file('second.png'),file('third.png')]);
  assert.match(h.$('vidRefPreview').innerHTML,/&lt;bad&gt;.png/);assert.doesNotMatch(h.$('vidRefPreview').innerHTML,/<bad>/);
  h.context.removeVideoReference(1);assert.deepEqual(h.revoked,['blob:fixture-1']);
  assert.match(h.$('vidRefPreview').innerHTML,/alt="图2"/);assert.doesNotMatch(h.$('vidRefPreview').innerHTML,/alt="图3"/);
  h.context.clearVideoReferences();assert.deepEqual(h.revoked,['blob:fixture-1','blob:fixture-0','blob:fixture-2']);
  assert.equal(h.$('vidRefPreview').innerHTML,'');assert.equal(h.$('vidRefClear').disabled,true);
  assert.equal(h.run('videoReferences.length'),0);
});

await check('page disposal revokes previews while back-forward cache retains them',async()=>{
  const h=harness();h.context.addVideoReferences([file('a.png')]);
  h.events.get('pagehide')({persisted:true});assert.equal(h.revoked.length,0);
  h.events.get('pagehide')({persisted:false});assert.deepEqual(h.revoked,['blob:fixture-0']);
});

await check('format, empty, count and combined byte validation retain previous selection',async()=>{
  const h=harness();h.context.addVideoReferences([file('a.png',{size:6*1024*1024})]);
  for(const f of [file('bad.gif',{type:'image/gif'}),file('empty.png',{size:0}),file('large.png',{size:5*1024*1024})])h.context.addVideoReferences([f]);
  h.context.addVideoReferences(Array.from({length:10},(_,i)=>file(i+'.png')));
  assert.equal(h.run('videoReferences.length'),1);assert.equal(h.created.length,1);assert.equal(h.messages.length,4);
  h.context.addVideoReferences([file('b.png',{size:4*1024*1024})]);assert.equal(h.run('videoReferences.length'),2);
  const full=harness();full.context.addVideoReferences(Array.from({length:10},(_,i)=>file(i+'.png')));
  assert.equal(full.run('videoReferences.length'),10);assert.equal(full.$('vidRefAdd').disabled,true);
});

await check('preview creation failure revokes partial batch without dropping prior files',async()=>{
  const h=harness();h.context.addVideoReferences([file('old.png')]);
  let count=0;h.context.URL.createObjectURL=()=>{if(++count===2)throw Error('fixture URL error');return 'blob:partial';};
  h.context.addVideoReferences([file('new.png'),file('bad.png')]);
  assert.equal(h.run('videoReferences.length'),1);assert.deepEqual(h.revoked,['blob:partial']);assert.equal(h.messages.length,1);
});

await check('read failures never create a text-only fallback task and restore controls',async()=>{
  for(const override of [{fail:true},{throwRead:true},{data:'bad-data-url'}]){
    const h=harness();h.context.addVideoReferences([file('bad.png',override)]);await h.context.runVideo();
    assert.equal(h.calls.length,0);assert.match(h.$('vidRes').innerHTML,/读取参考图片失败/);restored(h);
  }
});

await check('file read timeout aborts and restores the form without a request',async()=>{
  const h=harness();h.context.addVideoReferences([file('slow.png',{hold:true})]);const pending=h.context.runVideo();
  const timer=[...h.timers.values()].find(t=>t.delay===20000);assert.ok(timer);timer.callback();await pending;
  assert.equal(h.calls.length,0);assert.match(h.$('vidRes').innerHTML,/读取超时/);restored(h);
});

await check('duplicate clicks while request is pending create only one task',async()=>{
  const h=harness();let release;
  h.responses.push(()=>new Promise(resolve=>{release=resolve;}),completed);
  const pending=h.context.runVideo();await h.context.runVideo();assert.equal(h.calls.length,1);
  release({id:'task-1',status:'queued'});await pending;assert.equal(h.calls.filter(c=>c.opts?.method==='POST').length,1);restored(h);
});

await check('poll failure is bounded, shows task id, never recreates task, and restores controls',async()=>{
  const h=harness();h.responses.push({id:'task-1',status:'queued'},...Array.from({length:6},()=>new Error('temporary offline')));
  await h.context.runVideo();assert.equal(h.calls.length,7);assert.equal(h.calls.filter(c=>c.opts?.method==='POST').length,1);
  assert.match(h.$('vidRes').innerHTML,/状态查询暂时失败/);assert.match(h.$('vidRes').innerHTML,/task-1/);restored(h);
});

await check('transient poll failures recover, failed tasks terminate, and deadline is bounded',async()=>{
  const retry=harness();retry.responses.push({id:'task-1',status:'queued'},new Error('retry'),completed);
  await retry.context.runVideo();assert.match(retry.$('vidRes').innerHTML,/<video/);restored(retry);
  const failed=harness();failed.responses.push({id:'task-1',status:'queued'},{id:'task-1',status:'failed',error:'reference upload failed'});
  await failed.context.runVideo();assert.match(failed.$('vidRes').innerHTML,/reference upload failed/);restored(failed);
  const deadline=harness();deadline.responses.push({id:'task-1',status:'queued'},()=>{deadline.advance(15*60*1000);return {status:'processing'};});
  await deadline.context.runVideo();assert.equal(deadline.calls.length,2);assert.match(deadline.$('vidRes').innerHTML,/15 分钟/);restored(deadline);
});

await check('hung create request is aborted without an automatic duplicate POST',async()=>{
  const h=harness();h.responses.push((path,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(Error('aborted')))));
  const pending=h.context.runVideo();await flush();const timer=[...h.timers.values()].find(t=>t.delay===60000);assert.ok(timer);
  timer.callback();await pending;assert.equal(h.calls.length,1);assert.match(h.$('vidRes').innerHTML,/网络请求超时/);restored(h);
});

await check('invalid create responses and completed tasks missing URL restore controls',async()=>{
  for(const response of [{status:'queued'},{id:'task-1',status:'completed'}]){
    const h=harness();h.responses.push(response);await h.context.runVideo();assert.doesNotMatch(h.$('vidRes').innerHTML,/<video/);restored(h);
  }
});

console.log(`PASS: ${checks} video reference behavior checks; no account, network or generation`);
