"use strict";
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const original=fs.readFileSync(path.join(root,'dashboard/static/radio-editor.js'),'utf8');
const source=original.replace(/  \/\/ boot[\s\S]*$/,`  globalThis.testApi={formatMHz,parseMHz,inBands,changesFor,validationErrors,summary,canEdit,canReview,reviewDraft,writeDraft,renderTask,setSession,loadSnapshot,fetchSnapshot,refresh,startOperation,openAddChannel,confirmAddChannel,addChannel,requestDeleteChannel,confirmDeleteChannel,undoDeleteChannel,memoryMatches,memoryChangeCounts,renderControls,closeDialogs,configuredPairs,openRepeaterPresets,renderPresetChoices,renderPresetPreview,confirmRepeaterPreset,presetMemory,canRepairSavedUpload,showRepairConfirmation,repairSavedUpload,closeRepairConfirmation,selectModel,selectRole,confirmDiscard,selectedModel,
 setup(value,auth,config) { status=value; session=auth; connected=true; loadSnapshot(config); },
 draft(value){draft=value;}, status(value){status=value;}, role(value){role=value;}, model(value){externalModel=value;}, busy(value){busy=value;}, readAttempt(value){readAttempt=value;}, getReadAttempt(){return readAttempt;}, getSnapshot(){return snapshot;}, getDraft(){return draft;}, getProposal(){return proposal;}, count:changedCount};\n})();`);
const clone=value=>JSON.parse(JSON.stringify(value));
const labPairs=Array.from({length:6},(_,offset)=>({id:offset?'rpt'+offset:'default',label:'Lab pair '+(offset+1),index:offset+1,rx_name:'BOX-RX'+(offset+1),tx_name:'BOX-TX'+(offset+1),name:'BOX-RX'+(offset+1)+' / BOX-TX'+(offset+1),input_hz:145300000+offset*30000,output_hz:433500000+offset*25000,tone_hz:88.5}));
const memory={number:1,empty:false,name:'BOX-RX',rx_hz:145300000,tx_hz:null,mode:'NFM',power:'Low',skip:false,tx_tone:null,rx_tone:{type:'CTCSS',value:88.5}};
const snapshot={schema:1,role:'rx',base_id:'a'.repeat(32),read_at:'2026-10-28T20:00:00+00:00',model:'5RX',firmware:'HN5RV011',compatible:true,memories:[{number:0,empty:true},memory,{...memory,number:2,name:'FACTORY',tx_hz:462562500}],settings:{'settings.beep':false,'settings.workmode':'Channel','wmchannel.mrcha':1},capabilities:{bands_hz:[[136000000,174000000],[400000000,520000000]],tx_bands_hz:[[144000000,148000000],[420000000,450000000]],name_length:7,valid_characters:'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789- ',ctcss:[88.5,88.5],dcs:[23,25,26],settings:{'settings.beep':{label:'Beep',type:'boolean'},'settings.workmode':{label:'Mode',type:'select',options:['Frequency','Channel']},'wmchannel.mrcha':{label:'Memory A',type:'integer',min:0,max:127}}}};
function harness() {
 let focused=null,onSnapshot=null,onStatus=null;
 class Element {constructor(id=''){this.id=id;this.children=[];this.attrs={};this.listeners={};this.value='';this.checked=false;this.open=false;this.classList={toggle(){}};} setAttribute(k,v){this.attrs[k]=v;} removeAttribute(k){delete this.attrs[k];} addEventListener(type,listener){(this.listeners[type]??=[]).push(listener);} async dispatch(type){for(const listener of this.listeners[type]||[])await listener({target:this,preventDefault(){}});} append(...children){this.children.push(...children);} replaceChildren(...children){this.children=children;} showModal(){this.open=true;} close(){this.open=false;} focus(){focused=this.id;} scrollIntoView(){} }
 const elements=new Map();
 const find=(node,id)=>node.id===id?node:node.children.map(child=>find(child,id)).find(Boolean);
 const el=id=>{for(const root of elements.values()){const node=find(root,id);if(node)return node;}if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
 const calls=[];const status={admin:{authenticated:true},radio_programming:{available:true,configured:true,pending:false,roles:{rx:{},tx:{}}},software_updates:{pending:false}};
 let currentSnapshot=clone(snapshot);
 const ctx={console,URL,AbortController,setTimeout:()=>1,clearTimeout(){},document:{getElementById:el,createElement:()=>new Element()},fetch:async(url,options={})=>{calls.push({url,options});let data;if(url==='/api/status'){if(onStatus)await onStatus();data=clone(status);}else if(url.startsWith('/api/radio-editor?')){if(onSnapshot)await onSnapshot();const query=new URL(url,'http://localhost').searchParams;const match=currentSnapshot?.role===query.get('role')&&(currentSnapshot?.model||'5RX')===(query.get('model')||'5RX');data={editor:match?clone(currentSnapshot):null,radio_programming:clone(status.radio_programming)};}else if(url==='/api/radio-editor/read'){Object.assign(status.radio_programming,{pending:true,action:'editor-read',role:JSON.parse(options.body).role,request_id:'b'.repeat(32),success:null});data={radio_programming:clone(status.radio_programming)};}else if(url==='/api/radio-editor/write')data={radio_programming:{...status.radio_programming,pending:true,action:'editor-write',request_id:'b'.repeat(32)}};else if(url==='/api/radio-programming'){const body=JSON.parse(options.body);Object.assign(status.radio_programming,{pending:true,action:body.action,role:body.role,request_id:'f'.repeat(32),success:null});data={radio_programming:clone(status.radio_programming)};}else throw Error(url);return{ok:true,json:async()=>data};}};
 vm.createContext(ctx);vm.runInContext(source,ctx);ctx.testApi.setup(clone(status),{configured:true,authenticated:true,csrf_token:'token'},clone(snapshot));
 return{api:ctx.testApi,ctx,el,calls,status,get focused(){return focused;},set onSnapshot(value){onSnapshot=value;},set onStatus(value){onStatus=value;},setSnapshot(value){currentSnapshot=value;},draft(){return clone(ctx.testApi.getDraft());},posts:()=>calls.filter(call=>call.options.method==='POST')};
}
const checks=[];
async function check(name,fn){await fn();checks.push(name);}
function presetHarness(selectedRole='rx', mutate=()=>{}) {
 const h=harness();
 const value={...clone(snapshot),role:selectedRole,memories:Array.from({length:128},(_,number)=>number===1?clone(memory):{number,empty:true})};
 mutate(value);
 h.status.radio_programming.configured_roles=['rx','tx','programmer'];
 h.status.radio_programming.catalog_configured=true;h.status.radio_programming.catalog_digest='d'.repeat(64);h.status.radio_programming.pair_switching={presets:clone(labPairs)};
 h.status.radio_programming.roles.programmer={};
 h.api.role(selectedRole);
 h.api.setup(clone(h.status),{configured:true,authenticated:true,csrf_token:'token'},value);
 h.setSnapshot(value);
 return h;
}
function profileHarness(model='5RX', pending=false) {
 const h=presetHarness('programmer');
 h.status.radio_programming.supported_models={rx:['5RX'],tx:['5RX'],programmer:['5RX','UV5R']};
 h.status.radio_programming.roles.programmer={model,verification_required:pending,program_kind:pending?'editor':undefined,pending_id:pending?'e'.repeat(32):undefined};
 h.api.status(clone(h.status));h.api.model(model);h.api.renderTask();
 const value={...clone(snapshot),role:'programmer',model};
 if(model==='UV5R'){
  value.firmware='BFB297';value.settings={'settings.beep':false,'settings.squelch':3};
  value.capabilities.settings={'settings.beep':{label:'Beep',type:'boolean'},'settings.squelch':{label:'Squelch',type:'integer',min:0,max:9}};
  value.capabilities.bands_hz=[[136000000,174000000],[400000000,520000000]];
  value.capabilities.tx_bands_hz=[[144000000,148000000],[420000000,450000000]];
 }
 h.api.loadSnapshot(value);h.setSnapshot(value);return h;
}
function selectPreset(h,region,id,slot) {
 h.api.openRepeaterPresets();
 h.api.renderPresetChoices();
 h.el('preset-pair').value=id;
 h.api.renderPresetChoices();
 if(slot!==undefined)h.el('preset-slot').value=String(slot);
 h.api.renderPresetPreview();
}
function holdSnapshot(h) {
 let signalStarted,release;
 const started=new Promise(resolve=>{signalStarted=resolve;});
 const waiting=new Promise(resolve=>{release=resolve;});
 h.onSnapshot=()=>{signalStarted();return waiting;};
 return{started,release};
}
function repairHarness(selectedRole='rx',kind='editor') {
 const h=presetHarness(selectedRole);
 Object.assign(h.status.radio_programming,{request_id:'d'.repeat(32)});
 Object.assign(h.status.radio_programming.roles[selectedRole],{verification_required:true,verified:false,program_kind:kind,pending_id:'e'.repeat(32),compatible:true,firmware:'HN5RV011',identity:`known-${selectedRole}`});
 h.api.status(clone(h.status));h.api.renderTask();return h;
}
(async()=>{
 await check('MHz parsing keeps exact six-decimal precision and rejects malformed or zero values',()=>{const h=harness();assert.equal(h.api.parseMHz('145.300125'),145300125);assert.equal(h.api.formatMHz(145300125),'145.300125');for(const value of['','0','-145.3','145.3000001','1e8','145;reboot'])assert.equal(h.api.parseMHz(value),null);});
 await check('Unchanged snapshot produces no proposal changes',()=>{const h=harness();assert.equal(h.api.count(),0);assert.deepEqual(JSON.parse(JSON.stringify(h.api.changesFor(snapshot,snapshot))),{changes:[],settings:{}});});
 await check('Only edited memory and settings keys are sent',()=>{const h=harness();const draft=h.draft();draft.memories[1].name='LOCAL';draft.settings['settings.beep']=true;const changes=h.api.changesFor(snapshot,draft);assert.equal(changes.changes.length,1);assert.equal(changes.changes[0].number,1);assert.deepEqual(JSON.parse(JSON.stringify(changes.settings)),{'settings.beep':true});});
 await check('Unchanged factory transmit frequency is preserved for a name edit',()=>{const h=harness();const draft=h.draft();draft.memories[2].name='RENAMED';assert.equal(h.api.validationErrors(snapshot,draft).length,0);});
 await check('Changed transmit frequencies are checked against amateur transmit bands',()=>{const h=harness();const draft=h.draft();draft.memories[1].tx_hz=462562500;assert.match(h.api.validationErrors(snapshot,draft).join(' '),/amateur bands/);draft.memories[1].tx_hz=433500000;assert.equal(h.api.validationErrors(snapshot,draft).length,0);draft.memories[1].tx_hz=null;assert.equal(h.api.validationErrors(snapshot,draft).length,0);});
 await check('Radio frequencies require 10 Hz resolution and include upper band endpoints',()=>{const h=harness();const draft=h.draft();draft.memories[1].rx_hz=145300125;h.api.draft(draft);h.api.reviewDraft();assert.equal(h.el('review-dialog').open,false);assert.match(h.el('memory-error').textContent,/10 Hz/);assert.equal(h.posts().length,0);draft.memories[1].rx_hz=145300120;draft.memories[1].tx_hz=148000000;assert.equal(h.api.validationErrors(snapshot,draft).length,0);draft.memories[1].tx_hz=145300125;assert.match(h.api.validationErrors(snapshot,draft).join(' '),/transmit frequency/);});
 await check('New memory defaults to High power, NFM and transmit inhibited until explicitly configured',()=>{assert.match(original,/tx_hz: null, mode: "NFM", power: "High"/);});
 await check('Invalid receive frequency, unsupported names and tone values cannot be reviewed',()=>{const h=harness();const draft=h.draft();draft.memories[1].rx_hz=0;draft.memories[1].name='INVALID!';draft.memories[1].tx_tone={type:'DCS',value:999,polarity:'N'};h.api.draft(draft);h.api.reviewDraft();assert.equal(h.el('review-dialog').open,false);assert.match(h.el('memory-error').textContent,/receive frequency/);assert.equal(h.posts().length,0);});
 await check('Settings metadata restricts select, integer and boolean values',()=>{const h=harness();const draft=h.draft();draft.settings['settings.beep']='true';draft.settings['settings.workmode']='Unknown';draft.settings['wmchannel.mrcha']=128;assert.equal(h.api.validationErrors(snapshot,draft).length,3);});
 await check('Review lists the draft and does not send a write',()=>{const h=harness();const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);h.api.reviewDraft();assert.equal(h.el('review-dialog').open,true);assert.equal(h.el('change-list').children.length,1);assert.equal(h.posts().length,0);assert.equal(h.api.getProposal().base_id,snapshot.base_id);});
 await check('Channel review separates Before and After names from unchanged scan settings',()=>{
  const h=harness();const base=clone(snapshot);base.memories[1].name='BOX-RXX';h.api.loadSnapshot(base);
  const draft=h.draft();draft.memories[1].name='TEST123';h.api.draft(draft);h.api.reviewDraft();
  const item=h.el('change-list').children[0];assert.equal(item.children.length,3);
  const before=item.children[1],after=item.children[2];
  assert.equal(before.className,'editor-change-before');assert.equal(after.className,'editor-change-after');
  assert.match(before.textContent,/^Before: Name: BOX-RXX · RX .* · Scan: Include$/);
  assert.match(after.textContent,/^After: Name: TEST123 · RX .* · Scan: Include$/);
  assert.ok(!before.textContent.includes('TEST123'));assert.ok(!after.textContent.includes('BOX-RXX'));
  assert.ok(!before.textContent.includes('→'));assert.ok(!after.textContent.includes('→'));
  assert.equal(h.api.getProposal().changes.changes[0].name,'TEST123');
  assert.equal(h.api.getProposal().changes.changes[0].skip,false);assert.equal(h.posts().length,0);
 });
 await check('Radio-setting review also gives Before and After their own labelled lines',()=>{
  const h=harness();const draft=h.draft();draft.settings['settings.beep']=true;h.api.draft(draft);h.api.reviewDraft();
  const item=h.el('change-list').children[0];assert.equal(item.children[0].textContent,'Beep');
  assert.equal(item.children[1].textContent,'Before: false');assert.equal(item.children[2].textContent,'After: true');
  assert.equal(h.posts().length,0);
 });
 await check('Confirmed write binds role, snapshot, changed rows and admin CSRF',async()=>{const h=harness();const draft=h.draft();draft.memories[1].power='High';h.api.draft(draft);h.api.reviewDraft();await h.api.writeDraft();assert.equal(h.posts().length,1);const post=h.posts()[0];assert.equal(post.url,'/api/radio-editor/write');assert.equal(post.options.headers['X-CSRF-Token'],'token');const payload=JSON.parse(post.options.body);assert.equal(payload.role,'rx');assert.equal(payload.base_id,snapshot.base_id);assert.equal(payload.changes.length,1);assert.equal(payload.changes[0].power,'High');assert.equal(h.api.getSnapshot(),null);});
 await check('Changing the draft after review prevents a write',async()=>{const h=harness();let draft=h.draft();draft.memories[1].power='High';h.api.draft(draft);h.api.reviewDraft();draft=h.draft();draft.memories[1].name='OTHER';h.api.draft(draft);await h.api.writeDraft();assert.equal(h.posts().length,0);});
 await check('Server snapshot change prevents a stale reviewed write',async()=>{const h=harness();const draft=h.draft();draft.memories[1].power='High';h.api.draft(draft);h.api.reviewDraft();h.setSnapshot({...snapshot,base_id:'c'.repeat(32)});await h.api.writeDraft();assert.equal(h.posts().length,0);assert.match(h.el('review-error').textContent,/snapshot.*changed/);});
 await check('Typing a full seven-character name survives polling and sends exactly the reviewed name',async()=>{
  const h=harness();const name=h.el('memory-row-1').children[2].children[0];
  Object.assign(h.status.radio_programming,{action:'editor-read',success:true,role:'rx',request_id:snapshot.base_id});
  for(const value of['T','TE','TES','TEST','TEST-','TEST-R','TEST-RX']){
   name.value=value;await name.dispatch('input');await h.api.refresh();
   assert.equal(h.draft().memories[1].name,value);
   assert.equal(h.el('memory-row-1').children[2].children[0],name);
  }
  h.api.reviewDraft();assert.match(h.el('change-list').children[0].children[1].textContent,/^Before: Name: BOX-RX/);assert.match(h.el('change-list').children[0].children[2].textContent,/^After: Name: TEST-RX/);
  await h.api.writeDraft();assert.equal(h.posts().length,1);
  assert.equal(JSON.parse(h.posts()[0].options.body).changes[0].name,'TEST-RX');
 });
 await check('A completed explicit read fetches its new snapshot once and consumes its read attempt',async()=>{
  const h=harness();const next={...clone(snapshot),base_id:'b'.repeat(32)};h.setSnapshot(next);
  Object.assign(h.status.radio_programming,{action:'editor-read',success:true,role:'rx',request_id:next.base_id});
  h.api.readAttempt({role:'rx',priorRequestId:snapshot.base_id});
  await h.api.refresh();
  assert.equal(h.calls.filter(call=>call.url==='/api/radio-editor?role=rx').length,1);
  assert.equal(h.api.getSnapshot().base_id,next.base_id);assert.equal(h.api.getReadAttempt(),null);
  await h.api.refresh();
  assert.equal(h.calls.filter(call=>call.url==='/api/radio-editor?role=rx').length,1);
 });
 await check('A verified editor write loads its readback once for another edit without a manual read',async()=>{
  const h=harness();const draft=h.draft();draft.memories[1].name='TEST123';h.api.draft(draft);h.api.reviewDraft();await h.api.writeDraft();
  const next={...clone(snapshot),base_id:'b'.repeat(32)};next.memories[1].name='TEST123';h.setSnapshot(next);
  Object.assign(h.status.radio_programming,{action:'editor-write',pending:false,success:true,role:'rx',request_id:next.base_id});
  h.calls.length=0;await h.api.refresh();
  assert.equal(h.draft().memories[1].name,'TEST123');assert.equal(h.api.count(),0);
  assert.equal(h.calls.filter(call=>call.url==='/api/radio-editor?role=rx').length,1);
  await h.api.refresh();assert.equal(h.calls.filter(call=>call.url==='/api/radio-editor?role=rx').length,1);
  const restored=h.draft();restored.memories[1].name='BOX-RX';h.api.draft(restored);h.api.reviewDraft();await h.api.writeDraft();
  const payload=JSON.parse(h.posts()[0].options.body);assert.equal(payload.base_id,next.base_id);assert.equal(payload.changes[0].name,'BOX-RX');
 });
 await check('Pending, failed, different-role and newer-draft states never load a write as verified',async()=>{
  for(const change of[
   h=>{h.status.radio_programming.pending=true;},
   h=>{h.status.radio_programming.success=false;},
   h=>{h.status.radio_programming.role='tx';},
   h=>{const draft=h.draft();draft.memories[1].name='NEWEDIT';h.api.draft(draft);}
  ]){
   const h=harness();Object.assign(h.status.radio_programming,{action:'editor-write',pending:false,success:true,role:'rx',request_id:'b'.repeat(32)});change(h);
   await h.api.refresh();assert.equal(h.calls.filter(call=>call.url.startsWith('/api/radio-editor?')).length,0);
  }
 });
 await check('In-flight verification flags keep editor progress visible without failed-upload recovery advice',()=>{
  const h=repairHarness();Object.assign(h.status.radio_programming,{pending:true,success:null,action:'editor-write',phase:'verifying',message:'Reading back the radio.'});h.api.status(clone(h.status));h.api.renderTask();
  assert.equal(h.el('task-state').textContent,'Working');assert.equal(h.el('task-progress').hidden,false);
  assert.match(h.el('task-message').textContent,/Reading back.*audio remains paused/);
  assert.doesNotMatch(h.el('task-message').textContent,/Verify upload|repair|restart|power.cycle/i);
  Object.assign(h.status.radio_programming,{pending:false,success:false,message:'The readback did not match.'});h.api.status(clone(h.status));h.api.renderTask();
  assert.match(h.el('task-message').textContent,/Use Verify upload.*If the radio does not reconnect, restart/);
 });
 await check('An already loaded read completion does not force another fetch over a newer draft',async()=>{
  const h=harness();Object.assign(h.status.radio_programming,{action:'editor-read',success:true,role:'rx',request_id:snapshot.base_id});
  h.api.readAttempt({role:'rx',priorRequestId:'0'.repeat(32)});
  const name=h.el('memory-row-1').children[2].children[0];name.value='TEST-RX';await name.dispatch('input');
  await h.api.refresh();assert.equal(h.calls.filter(call=>call.url.startsWith('/api/radio-editor?')).length,0);
  assert.equal(h.draft().memories[1].name,'TEST-RX');assert.equal(h.api.getReadAttempt(),null);
 });
 await check('Typing while a forced snapshot GET is pending preserves the new draft and input node',async()=>{
  const h=harness();h.setSnapshot({...clone(snapshot),base_id:'b'.repeat(32)});
  const hold=holdSnapshot(h);const pending=h.api.fetchSnapshot(true);await hold.started;
  const name=h.el('memory-row-1').children[2].children[0];name.value='TEST-RX';await name.dispatch('input');
  hold.release();await pending;
  assert.equal(h.api.getSnapshot().base_id,snapshot.base_id);assert.equal(h.draft().memories[1].name,'TEST-RX');
  assert.equal(h.el('memory-row-1').children[2].children[0],name);assert.equal(h.posts().length,0);
 });
 await check('A forced response cannot replace a snapshot loaded more recently during its GET',async()=>{
  const h=harness();h.setSnapshot({...clone(snapshot),base_id:'b'.repeat(32)});
  const hold=holdSnapshot(h);const pending=h.api.fetchSnapshot(true);await hold.started;
  const newer={...clone(snapshot),base_id:'c'.repeat(32)};newer.memories[1].name='NEWER';h.api.loadSnapshot(newer);
  hold.release();await pending;
  assert.equal(h.api.getSnapshot().base_id,newer.base_id);assert.equal(h.draft().memories[1].name,'NEWER');
 });
 await check('Role and admin-session changes prevent an in-flight forced snapshot from loading',async()=>{
  for(const change of[
   h=>h.api.role('tx'),
   h=>h.api.setSession({configured:true,authenticated:true,csrf_token:'new-token'}),
   h=>h.api.setSession({configured:true,authenticated:false,csrf_token:null})
  ]){
   const h=harness();h.setSnapshot({...clone(snapshot),base_id:'b'.repeat(32)});
   const hold=holdSnapshot(h);const pending=h.api.fetchSnapshot(true);await hold.started;change(h);hold.release();await pending;
   assert.notEqual(h.api.getSnapshot()?.base_id,'b'.repeat(32));assert.equal(h.posts().length,0);
  }
 });
 await check('A confirmed manual Read still discards its previous draft and loads the completed radio read',async()=>{
  const h=harness();const previous=h.draft();previous.memories[1].name='UNSAVED';h.api.draft(previous);
  h.el('read-dialog').showModal();await h.api.startOperation('read');
  assert.equal(h.posts().length,1);assert.equal(h.posts()[0].url,'/api/radio-editor/read');assert.equal(h.api.getDraft(),null);
  const next={...clone(snapshot),base_id:'b'.repeat(32)};next.memories[1].name='FRESH';h.setSnapshot(next);
  Object.assign(h.status.radio_programming,{pending:false,success:true});await h.api.refresh();
  assert.equal(h.draft().memories[1].name,'FRESH');assert.equal(h.api.getReadAttempt(),null);
  assert.equal(h.calls.filter(call=>call.url==='/api/radio-editor?role=rx').length,1);
 });
 await check('A Read without its confirmation does not discard a draft or send a request',async()=>{
  const h=harness();const draft=h.draft();draft.memories[1].name='UNSAVED';h.api.draft(draft);
  await h.api.startOperation('read');assert.equal(h.draft().memories[1].name,'UNSAVED');assert.equal(h.posts().length,0);
 });
 await check('Draft edits made during the final pre-write GET invalidate the old reviewed proposal',async()=>{
  const h=harness();const name=h.el('memory-row-1').children[2].children[0];name.value='TEST-RX';await name.dispatch('input');h.api.reviewDraft();
  const hold=holdSnapshot(h);const pending=h.api.writeDraft();await hold.started;
  name.value='LATER';await name.dispatch('input');
  hold.release();await pending;
  assert.equal(h.draft().memories[1].name,'LATER');assert.equal(h.posts().length,0);
  assert.match(h.el('review-error').textContent,/draft.*changed/);
 });
 await check('An admin token change during the final pre-write GET invalidates the old reviewed proposal',async()=>{
  const h=harness();const draft=h.draft();draft.memories[1].name='TEST-RX';h.api.draft(draft);h.api.reviewDraft();
  const hold=holdSnapshot(h);const pending=h.api.writeDraft();await hold.started;
  h.api.setSession({configured:true,authenticated:true,csrf_token:'new-token'});hold.release();await pending;
  assert.equal(h.posts().length,0);assert.equal(h.draft().memories[1].name,'TEST-RX');
  assert.match(h.el('review-error').textContent,/session.*changed/);
 });
 await check('Active tasks prevent editing and review',()=>{const h=harness();h.status.radio_programming.pending=true;h.api.status(h.status);const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);assert.equal(h.api.canEdit(),false);h.api.reviewDraft();assert.equal(h.el('review-dialog').open,false);});
 await check('Other radio verification permits reading, editing and reviewing a selected radio',()=>{const h=harness();h.status.radio_programming.roles.tx.verification_required=true;h.api.status(h.status);const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);h.api.renderTask();assert.equal(h.el('read-radio').disabled,false);assert.equal(h.api.canEdit(),true);h.api.reviewDraft();assert.equal(h.el('review-dialog').open,true);assert.match(h.el('task-message').textContent,/audio remains paused/);});
 await check('Own unfinished upload permits reading and drafting but requires fresh recovery metadata to review',()=>{const h=harness();h.status.radio_programming.roles.rx.verification_required=true;h.api.status(h.status);const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);h.api.renderTask();assert.equal(h.el('read-radio').disabled,false);assert.equal(h.api.canEdit(),true);assert.equal(h.api.canReview(),false);h.api.reviewDraft();assert.equal(h.el('review-dialog').open,false);});
 await check('A repair can explicitly adopt unchanged current values with its exact previous candidate token',async()=>{const h=harness();h.status.radio_programming.roles.rx.verification_required=true;h.api.status(h.status);const recovery={required:true,pending_id:'d'.repeat(32),protected_mismatch:false,message:'Automatic lock: expected True, read False',differences:[{field:'settings.beep',expected:'True',actual:'False'}]};const value={...clone(snapshot),recovery};h.api.loadSnapshot(value);h.setSnapshot(value);assert.equal(h.api.count(),0);assert.equal(h.api.canReview(),true);h.api.reviewDraft();assert.equal(h.el('review-dialog').open,true);assert.equal(h.el('write-confirm').textContent,'Adopt current values');assert.equal(h.el('review-recovery-warning').hidden,false);assert.match(h.el('review-recovery-differences').children[0].children[1].textContent,/Previous candidate: True.*Radio read: False.*Desired repair value: false/);assert.equal(h.posts().length,0);await h.api.writeDraft();assert.equal(h.posts().length,1);const payload=JSON.parse(h.posts()[0].options.body);assert.equal(payload.supersede_id,'d'.repeat(32));assert.deepEqual(payload.changes,[]);assert.deepEqual(payload.settings,{});});
 await check('A changed unfinished candidate invalidates a reviewed repair',async()=>{const h=harness();h.status.radio_programming.roles.rx.verification_required=true;h.api.status(h.status);const value={...clone(snapshot),recovery:{required:true,pending_id:'d'.repeat(32),protected_mismatch:false,differences:[]}};h.api.loadSnapshot(value);h.setSnapshot({...value,recovery:{...value.recovery,pending_id:'e'.repeat(32)}});h.api.reviewDraft();await h.api.writeDraft();assert.equal(h.posts().length,0);assert.match(h.el('review-error').textContent,/unfinished candidate changed/);});
 await check('Protected data mismatch permits inspection but blocks every repair proposal',()=>{const h=harness();h.status.radio_programming.roles.rx.verification_required=true;h.api.status(h.status);h.api.loadSnapshot({...clone(snapshot),recovery:{required:true,pending_id:'d'.repeat(32),protected_mismatch:true,differences:[]}});const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);assert.equal(h.api.canEdit(),true);assert.equal(h.api.canReview(),false);h.api.reviewDraft();assert.equal(h.el('review-dialog').open,false);assert.match(h.el('recovery-detail').textContent,/Protected radio data/);});
 await check('External programmer choice appears only after explicit local configuration',()=>{const h=harness();h.api.renderTask();assert.deepEqual(h.el('radio-role').children.map(child=>child.value),['rx','tx']);h.status.radio_programming.configured_roles=['rx','tx','programmer'];h.status.radio_programming.roles.programmer={};h.api.status(h.status);h.api.renderTask();assert.deepEqual(h.el('radio-role').children.map(child=>child.value),['rx','tx','programmer']);assert.equal(h.el('radio-role').children[2].textContent,'External programmer');h.api.role('programmer');h.api.loadSnapshot({...clone(snapshot),role:'programmer'});assert.equal(h.api.canEdit(),true);h.status.radio_programming.configured_roles=['rx','tx'];h.api.status(h.status);assert.equal(h.api.canEdit(),false);});
 await check('Unknown or incompatible profile cannot create a write proposal',()=>{for(const compatible of[false,undefined]){const h=harness();h.api.loadSnapshot({...clone(snapshot),compatible});const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);assert.equal(h.api.canEdit(),false);h.api.reviewDraft();assert.equal(h.el('review-dialog').open,false);assert.equal(h.posts().length,0);assert.match(h.el('snapshot-detail').textContent,/read-only/);}});
 await check('Signing out clears the sensitive snapshot and closes review',()=>{const h=harness();const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);h.api.reviewDraft();h.api.setSession({configured:true,authenticated:false,csrf_token:null});assert.equal(h.api.getSnapshot(),null);assert.equal(h.el('review-dialog').open,false);assert.equal(h.el('editor-workspace').hidden,true);assert.equal(h.el('memory-rows').children.length,0);});
 await check('Progress shows actual phase block counts and escaped text activity',()=>{const h=harness();h.status.radio_programming={...h.status.radio_programming,pending:true,phase:'reading',progress:{current:8,total:128,percent:6,unit:'blocks'},events:[{time:'2026-10-28T20:00:00Z',message:'<connected>'}]};h.api.status(h.status);h.api.renderTask();assert.equal(h.el('task-progress').hidden,false);assert.equal(h.el('task-progress-bar').value,8);assert.equal(h.el('task-progress-bar').max,128);assert.equal(h.el('task-progress-detail').textContent,'8 / 128 blocks');assert.match(h.el('task-events').children[0].textContent,/<connected>/);});
 await check('Add channel lists empty slots and reveals a local draft with a required receive frequency',()=>{const h=harness();h.el('used-only').checked=true;h.el('memory-search').value='FACTORY';h.api.openAddChannel();assert.equal(h.el('add-channel-dialog').open,true);assert.deepEqual(h.el('add-channel-slot').children.map(option=>option.value),['0']);h.api.confirmAddChannel();assert.equal(h.el('add-channel-dialog').open,false);const added=h.draft().memories[0];assert.equal(added.empty,false);assert.equal(added.power,'High');assert.equal(added.mode,'NFM');assert.equal(added.tx_hz,null);assert.equal(added.rx_hz,0);assert.equal(h.el('used-only').checked,false);assert.equal(h.el('memory-search').value,'');assert.equal(h.focused,'memory-rx-0');assert.equal(h.posts().length,0);h.api.reviewDraft();assert.equal(h.el('review-dialog').open,false);assert.match(h.el('memory-error').textContent,/receive frequency/);});
 await check('Add to an occupied or invalid memory cannot replace an existing channel',()=>{const h=harness();assert.equal(h.api.addChannel(1),false);assert.equal(h.api.addChannel(128),false);assert.equal(h.api.addChannel(-1),false);assert.equal(h.api.count(),0);assert.equal(h.posts().length,0);});
 await check('Full radio disables Add with a useful capacity message',()=>{const h=harness();const full={...clone(snapshot),memories:Array.from({length:128},(_,number)=>({...clone(memory),number}))};h.api.loadSnapshot(full);assert.equal(h.el('add-channel').disabled,true);assert.match(h.el('channel-capacity').textContent,/All 128 memories are in use/);h.api.openAddChannel();assert.equal(h.el('add-channel-dialog').open,false);assert.equal(h.posts().length,0);});
 await check('Deleting names the exact channel and changes nothing before confirmation',()=>{const h=harness();h.api.requestDeleteChannel(1);assert.equal(h.el('delete-channel-dialog').open,true);assert.equal(h.el('delete-channel-title').textContent,'Delete memory 1?');assert.match(h.el('delete-channel-description').textContent,/BOX-RX.*145\.300000 MHz/);assert.equal(h.draft().memories[1].empty,false);assert.equal(h.posts().length,0);h.el('delete-channel-dialog').close();h.api.confirmDeleteChannel();assert.equal(h.draft().memories[1].empty,false);});
 await check('Confirmed deletion stays visible with Used-only enabled and Undo restores all draft edits',()=>{const h=harness();const draft=h.draft();draft.memories[1].name='EDITED';draft.memories[1].power='High';h.api.draft(draft);h.el('used-only').checked=true;h.api.requestDeleteChannel(1);h.api.confirmDeleteChannel();assert.deepEqual(h.draft().memories[1],{number:1,empty:true});assert.equal(h.api.memoryMatches(h.draft().memories[1]),true);assert.equal(h.el('memory-action-1').textContent,'Undo');assert.match(h.el('memory-row-1').children[2].children[0].textContent,/EDITED.*Undo restores/);assert.equal(h.posts().length,0);h.api.undoDeleteChannel(1);assert.equal(h.draft().memories[1].name,'EDITED');assert.equal(h.draft().memories[1].power,'High');assert.equal(h.draft().memories[1].rx_tone.value,88.5);assert.equal(h.posts().length,0);});
 await check('Add at a deleted slot creates a fresh channel while Undo remains a distinct restore action',()=>{const h=harness();const draft=h.draft();draft.memories[1].name='EDITED';h.api.draft(draft);h.api.requestDeleteChannel(1);h.api.confirmDeleteChannel();h.api.openAddChannel();assert.match(h.el('add-channel-slot').children[1].textContent,/deleted in draft/);h.el('add-channel-slot').value='1';h.api.confirmAddChannel();const added=h.draft().memories[1];assert.equal(added.name,'');assert.equal(added.rx_hz,0);assert.equal(added.tx_hz,null);assert.equal(added.power,'High');assert.equal(added.mode,'NFM');assert.equal(h.posts().length,0);});
 await check('Added and deleted channels are counted and reviewed before one authenticated write',async()=>{const h=harness();h.api.addChannel(0);const rx=h.el('memory-rx-0');rx.value='146.520000';await rx.dispatch('input');h.api.requestDeleteChannel(1);h.api.confirmDeleteChannel();h.api.reviewDraft();assert.equal(h.el('review-change-counts').textContent.trim(),'1 added · 1 deleted · 0 updated');assert.equal(h.el('change-list').children[0].children[0].textContent,'Added · Memory 0');assert.equal(h.el('change-list').children[1].children[0].textContent,'Deleted · Memory 1');assert.match(h.el('change-list').children[1].children[2].textContent,/^After: Clear this memory on the radio/);assert.equal(h.posts().length,0);await h.api.writeDraft();assert.equal(h.posts().length,1);assert.equal(h.posts()[0].options.headers['X-CSRF-Token'],'token');const payload=JSON.parse(h.posts()[0].options.body);assert.equal(payload.changes[0].rx_hz,146520000);assert.deepEqual(payload.changes[1],{number:1,empty:true});});
 await check('Active work and signing out prevent forced Add or Delete confirmation',()=>{const h=harness();h.api.openAddChannel();h.api.requestDeleteChannel(1);h.status.radio_programming.pending=true;h.api.status(h.status);h.api.confirmAddChannel();h.api.confirmDeleteChannel();assert.equal(h.api.count(),0);h.api.setSession({configured:true,authenticated:false,csrf_token:null});assert.equal(h.el('add-channel-dialog').open,false);assert.equal(h.el('delete-channel-dialog').open,false);h.api.confirmDeleteChannel();assert.equal(h.posts().length,0);});
 await check('Private pair labels accept the shared schema limit and reject oversized entries',()=>{
  for(const size of[60,61,80,81]){
   const h=presetHarness();h.status.radio_programming.pair_switching.presets[0].label='L'.repeat(size);h.api.status(h.status);
   assert.equal(h.api.configuredPairs().some(pair=>pair.id==='default'),size<=80);
  }
 });
 await check('The editor uses only the private catalog supplied by this Pi',()=>{
  const h=presetHarness();assert.deepEqual(clone(h.api.configuredPairs()),labPairs);
  const missing=harness();assert.deepEqual(clone(missing.api.configuredPairs()),[]);
  missing.api.openRepeaterPresets();assert.equal(missing.el('repeater-presets-dialog').open,false);
 });
 await check('Private pairs map indexed RX, TX and handheld names with configured tone directions',()=>{
  const h=presetHarness();
  for(const pair of labPairs)for(const selectedRole of['rx','tx','programmer']){
   const result=clone(h.api.presetMemory(pair,pair.index+1,selectedRole));
   assert.equal(result.name,selectedRole==='rx'?pair.rx_name:selectedRole==='tx'?pair.tx_name:'BOX-CH'+pair.index);
   assert.equal(result.number,pair.index+1);assert.equal(result.power,'High');assert.equal(result.mode,'NFM');
   assert.equal(result.rx_hz,selectedRole==='rx'?pair.input_hz:pair.output_hz);
   assert.equal(result.tx_hz,selectedRole==='rx'?null:selectedRole==='tx'?pair.output_hz:pair.input_hz);
   assert.deepEqual(result.tx_tone,selectedRole==='rx'?null:{type:'CTCSS',value:88.5});
   assert.deepEqual(result.rx_tone,selectedRole==='rx'?{type:'CTCSS',value:88.5}:null);
  }
 });
 await check('Private pairs stage drafts for all three radio roles without touching memory 1 or global settings',()=>{
  for(const selectedRole of['rx','tx','programmer']){
   const h=presetHarness(selectedRole);
   const before=h.draft();
   selectPreset(h,'private','rpt5');
   assert.equal(h.el('repeater-presets-dialog').open,true);
   assert.equal(h.el('preset-slot').value,'7');
   h.api.confirmRepeaterPreset();
   const after=h.draft();
   assert.equal(h.api.count(),1);
   assert.deepEqual(after.memories[1],before.memories[1]);
   assert.deepEqual(after.settings,before.settings);
   assert.equal(after.memories[7].empty,false);
   assert.equal(h.el('review-dialog').open,false);
   assert.equal(h.posts().length,0);
  }
 });
 await check('Adding all six private pairs preserves earlier draft changes and the documented matching slots',()=>{
  const h=presetHarness();
  const value=h.draft();value.settings['settings.beep']=true;value.memories[1].name='KEEP';h.api.draft(value);
  for(const pair of labPairs){
   selectPreset(h,"private",pair.id);
   assert.equal(h.el('preset-slot').value,String(pair.index+1));
   h.api.confirmRepeaterPreset();
  }
  const after=h.draft();
  assert.equal(after.settings['settings.beep'],true);
  assert.equal(after.memories[1].name,'KEEP');
  assert.deepEqual(after.memories.slice(2,8).map(item=>item.empty),[false,false,false,false,false,false]);
  assert.equal(h.api.count(),8);
  assert.equal(h.posts().length,0);
 });
 await check('Occupied preferred slots fall back to an empty slot and cannot be forcibly overwritten',()=>{
  const h=presetHarness('rx',value=>{value.memories[2]={...clone(memory),number:2,name:'KEEP'};});
  selectPreset(h,'private','default');
  assert.equal(h.el('preset-slot').value,'3');
  assert.ok(!h.el('preset-slot').children.some(option=>option.value==='2'));
  h.el('preset-slot').value='2';h.api.confirmRepeaterPreset();
  assert.equal(h.api.count(),0);
  assert.equal(h.draft().memories[2].name,'KEEP');
  assert.equal(h.posts().length,0);
 });
 await check('Memory 1 stays excluded even when empty and a deleted original memory is not available for presets',()=>{
  const h=presetHarness('rx',value=>{value.memories[1]={number:1,empty:true};value.memories[2]={...clone(memory),number:2};});
  h.api.requestDeleteChannel(2);h.api.confirmDeleteChannel();
  selectPreset(h,'private','default');
  const choices=h.el('preset-slot').children.map(option=>option.value);
  assert.ok(!choices.includes('1'));
  assert.ok(!choices.includes('2'));
  assert.equal(h.el('preset-slot').value,'3');
  const before=h.draft();h.el('preset-slot').value='1';h.api.confirmRepeaterPreset();
  assert.deepEqual(h.draft(),before);
  assert.equal(h.posts().length,0);
 });
 await check('A full radio cannot stage presets or reuse slots occupied when the radio was read',()=>{
  const h=presetHarness('rx',value=>{value.memories=Array.from({length:128},(_,number)=>({...clone(memory),number}));});
  h.api.renderControls();
  assert.equal(h.el('repeater-presets').disabled,true);
  h.api.openRepeaterPresets();
  assert.equal(h.el('repeater-presets-dialog').open,false);
  h.api.requestDeleteChannel(7);h.api.confirmDeleteChannel();
  h.api.renderControls();
  assert.equal(h.el('repeater-presets').disabled,true);
  h.api.openRepeaterPresets();
  assert.equal(h.el('repeater-presets-dialog').open,false);
  assert.equal(h.api.count(),1);
  assert.equal(h.posts().length,0);
 });
 await check('Memory 0 is a last available fallback while memory 1 remains reserved',()=>{
  const h=presetHarness('rx',value=>{value.memories=Array.from({length:128},(_,number)=>number===0?{number,empty:true}:{...clone(memory),number});});
  selectPreset(h,'private','rpt3');
  assert.equal(h.el('preset-slot').value,'0');
  assert.deepEqual(h.el('preset-slot').children.map(option=>option.value),['0']);
  h.api.confirmRepeaterPreset();
  assert.equal(h.draft().memories[0].empty,false);
  assert.equal(h.draft().memories[1].rx_hz,memory.rx_hz);
  assert.equal(h.posts().length,0);
 });
 await check('Removing or changing a private catalog invalidates an open pair draft confirmation',()=>{
  for(const change of['missing','changed']){
   const h=presetHarness();selectPreset(h,'private','default',2);const before=h.draft();
   if(change==='missing')h.status.radio_programming.catalog_configured=false;else h.status.radio_programming.catalog_digest='e'.repeat(64);
   h.api.status(h.status);h.api.renderControls();h.api.confirmRepeaterPreset();assert.deepEqual(h.draft(),before);assert.equal(h.posts().length,0);
  }
 });
 await check('Preset frequencies and tones must fit the radio capabilities before any draft changes are applied',()=>{
  const variants=[
   value=>{value.capabilities.ctcss=[67];},
   value=>{value.capabilities.bands_hz=[[400000000,520000000]];},
   value=>{value.capabilities.name_length=1;}
  ];
  for(const mutate of variants){
   const h=presetHarness('rx',mutate);selectPreset(h,'private','default');
   const before=h.draft();h.api.confirmRepeaterPreset();
   assert.deepEqual(h.draft(),before);
   assert.equal(h.posts().length,0);
  }
  const h=presetHarness('programmer',value=>{value.capabilities.tx_bands_hz=[[420000000,450000000]];});
  selectPreset(h,'private','rpt3');h.api.confirmRepeaterPreset();
  assert.equal(h.api.count(),0);
 });
 await check('Invalid unrelated draft edits block preset staging while preserving the current draft',()=>{
  const h=presetHarness();const draft=h.draft();draft.memories[1].rx_hz=0;h.api.draft(draft);
  selectPreset(h,'private','default');const before=h.draft();h.api.confirmRepeaterPreset();
  assert.deepEqual(h.draft(),before);
  assert.match(h.el('preset-error').textContent,/receive frequency/);
  assert.equal(h.posts().length,0);
 });
 await check('Preset confirmation rechecks active maintenance and local busy state',()=>{
  for(const operation of['radio_programming','software_updates','wifi_visibility','busy']){
   const h=presetHarness();selectPreset(h,'private','default');
   if(operation==='busy')h.api.busy(true);else{h.status[operation]={...h.status[operation],pending:true};h.api.status(h.status);}
   h.api.renderControls();
   assert.equal(h.el('repeater-presets-confirm').disabled,true);
   h.api.confirmRepeaterPreset();assert.equal(h.api.count(),0);assert.equal(h.posts().length,0);
  }
 });
 await check('Read-only snapshots, stale sessions, changed roles and closed dialogs cannot stage presets',()=>{
  const h=presetHarness('rx',value=>{value.compatible=false;});
  h.api.openRepeaterPresets();assert.equal(h.el('repeater-presets-dialog').open,false);
  for(const mutate of[
   current=>{current.api.setSession({configured:true,authenticated:true,csrf_token:'new-token'});},
   current=>{current.api.role('tx');},
   current=>{current.el('repeater-presets-dialog').close();}
  ]){
   const current=presetHarness();selectPreset(current,'private','default');mutate(current);
   current.api.confirmRepeaterPreset();assert.equal(current.api.count(),0);assert.equal(current.posts().length,0);
  }
 });
 await check('A memory occupied after opening the preset dialog is preserved at confirmation',()=>{
  const h=presetHarness();selectPreset(h,'private','default');
  const draft=h.draft();draft.memories[2]={...clone(memory),number:2,name:'DRAFT'};h.api.draft(draft);
  h.api.confirmRepeaterPreset();assert.deepEqual(h.draft(),draft);assert.equal(h.posts().length,0);
 });
 await check('Unknown configured pairs and a newly read snapshot invalidate preset confirmation',()=>{
  const h=presetHarness();selectPreset(h,'private','default');
  h.el('preset-pair').value='UNLISTED';h.api.confirmRepeaterPreset();
  assert.equal(h.api.count(),0);assert.equal(h.posts().length,0);
  selectPreset(h,'private','default');
  h.api.loadSnapshot({...clone(h.api.getSnapshot()),base_id:'e'.repeat(32)});
  h.api.confirmRepeaterPreset();assert.equal(h.api.count(),0);assert.equal(h.posts().length,0);
 });
 await check('Preset additions use the existing review and authenticated final write confirmation',async()=>{
  const h=presetHarness();selectPreset(h,'private','default');h.api.confirmRepeaterPreset();
  assert.equal(h.posts().length,0);h.api.reviewDraft();
  assert.equal(h.el('review-dialog').open,true);assert.equal(h.posts().length,0);
  assert.match(h.el('review-change-counts').textContent,/1 added/);
  await h.api.writeDraft();assert.equal(h.posts().length,1);
  const post=h.posts()[0],payload=JSON.parse(post.options.body);
  assert.equal(post.url,'/api/radio-editor/write');assert.equal(post.options.headers['X-CSRF-Token'],'token');
  assert.equal(payload.role,'rx');assert.equal(payload.base_id,snapshot.base_id);
  assert.equal(payload.changes.length,1);assert.equal(payload.changes[0].number,2);
  assert.deepEqual(payload.settings,{});
 });
 await check('Signing out closes the preset dialog and prevents a forced addition',()=>{
  const h=presetHarness();selectPreset(h,'private','default');
  h.api.setSession({configured:true,authenticated:false,csrf_token:null});
  assert.equal(h.el('repeater-presets-dialog').open,false);h.api.confirmRepeaterPreset();
  assert.equal(h.api.getDraft(),null);assert.equal(h.posts().length,0);
 });
 for(const selectedRole of['rx','tx','programmer'])for(const kind of['editor','repeater'])await check(`Saved ${kind} repair for ${selectedRole} reviews and sends only that connection's pending candidate`,async()=>{
  const h=repairHarness(selectedRole,kind);
  assert.equal(h.el('repair-radio').hidden,false);assert.equal(h.el('repair-radio').disabled,false);
  h.api.showRepairConfirmation();assert.equal(h.el('repair-dialog').open,true);assert.equal(h.focused,'repair-cancel');
  assert.match(h.el('repair-title').textContent,new RegExp(selectedRole==='programmer'?'external programmer':'^Repair the saved '+selectedRole.toUpperCase()));
  assert.match(h.el('repair-description').textContent,/private saved candidate.*neighboring bytes.*does not apply a new default plan/);
  assert.equal(h.posts().length,0);await h.api.repairSavedUpload();
  assert.equal(h.posts().length,1);assert.equal(h.calls[0].url,'/api/status');
  assert.equal(h.posts()[0].url,'/api/radio-programming');assert.equal(h.posts()[0].options.headers['X-CSRF-Token'],'token');
  assert.deepEqual(JSON.parse(h.posts()[0].options.body),{role:selectedRole,action:'repair-upload',pending_id:'e'.repeat(32)});
  assert.equal(h.api.getSnapshot(),null);assert.equal(h.api.getDraft(),null);assert.equal(h.el('repair-dialog').open,false);
  assert.equal(h.el('read-radio').disabled,true);assert.equal(h.el('repair-radio').disabled,true);
 });
 await check('Repair visibility requires a supported selected pending candidate and refuses paired recovery',async()=>{
  for(const mutate of[
   h=>{h.status.radio_programming.roles.rx.verification_required=false;},
   h=>{delete h.status.radio_programming.roles.rx.pending_id;},
   h=>{h.status.radio_programming.roles.rx.pending_id='invalid';},
   h=>{h.status.radio_programming.roles.rx.program_kind='pair';},
   h=>{h.status.radio_programming.pair={verification_required:true};},
   h=>{Object.assign(h.status.radio_programming.roles.tx,{verification_required:true,program_kind:'pair'});},
   h=>{h.status.radio_programming.configured_roles=['tx','programmer'];}
  ]){
   const h=repairHarness();mutate(h);h.api.status(clone(h.status));h.api.renderControls();
   assert.equal(h.el('repair-radio').hidden,true);assert.equal(h.el('repair-radio').disabled,true);
   h.api.showRepairConfirmation();await h.api.repairSavedUpload();assert.equal(h.el('repair-dialog').open,false);assert.equal(h.posts().length,0);
  }
 });
 await check('Repairing a saved upload clearly excludes the browser draft and Cancel preserves that draft',async()=>{
  const h=repairHarness();const draft=h.draft();draft.memories[1].name='UNSAVED';h.api.draft(draft);
  h.api.showRepairConfirmation();assert.equal(h.el('repair-draft-warning').hidden,false);
  h.api.closeRepairConfirmation();await h.api.repairSavedUpload();
  assert.deepEqual(h.draft(),draft);assert.equal(h.focused,'repair-radio');assert.equal(h.posts().length,0);
 });
 for(const [name,mutate]of[
  ['pending candidate changed',h=>{h.status.radio_programming.roles.rx.pending_id='c'.repeat(32);}],
  ['upload kind changed',h=>{h.status.radio_programming.roles.rx.program_kind='repeater';}],
  ['request changed',h=>{h.status.radio_programming.request_id='c'.repeat(32);}],
  ['firmware changed',h=>{h.status.radio_programming.roles.rx.firmware='other';}],
  ['radio identity changed',h=>{h.status.radio_programming.roles.rx.identity='other';}],
  ['verification resolved',h=>{h.status.radio_programming.roles.rx.verification_required=false;}],
  ['paired recovery appeared',h=>{h.status.radio_programming.pair={verification_required:true};}],
  ['radio task started',h=>{h.status.radio_programming.pending=true;}],
  ['software update started',h=>{h.status.software_updates.pending=true;}],
  ['Wi-Fi change started',h=>{h.status.wifi_visibility={pending:true};}],
  ['session signed out',h=>{h.status.admin.authenticated=false;}],
  ['session token changed',h=>{h.onStatus=()=>h.api.setSession({configured:true,authenticated:true,csrf_token:'new-token'});}]
 ])await check(`Saved-upload repair is not sent when ${name} after review`,async()=>{
  const h=repairHarness();h.api.showRepairConfirmation();mutate(h);await h.api.repairSavedUpload();
  assert.equal(h.posts().length,0);assert.equal(h.el('repair-dialog').open,false);
  assert.match(h.el('task-message').textContent,/changed/);
 });
 await check('Repair rechecks draft and selected role after a status request is already in flight',async()=>{
  for(const changeRole of[false,true]){
   const h=repairHarness();h.api.showRepairConfirmation();
   h.onStatus=()=>{if(changeRole)h.api.role('programmer');else{const draft=h.draft();draft.memories[1].name='NEWDRAF';h.api.draft(draft);}};
   await h.api.repairSavedUpload();assert.equal(h.posts().length,0);
   if(!changeRole)assert.equal(h.draft().memories[1].name,'NEWDRAF');
  }
 });
 await check('Unconfigured helper, preview, signed-out session and maintenance reject forced editor repairs',async()=>{
  for(const mutate of[
   h=>{h.status.radio_programming.available=false;},h=>{h.status.radio_programming.configured=false;},
   h=>{h.status.radio_programming.preview=true;},h=>{h.status.radio_programming.pending=true;},
   h=>{h.status.software_updates.pending=true;},h=>{h.status.wifi_visibility={pending:true};},
   h=>{h.api.busy(true);},h=>{h.api.setSession({configured:true,authenticated:false,csrf_token:null});}
  ]){
   const h=repairHarness();mutate(h);h.api.status(clone(h.status));h.api.renderControls();
   assert.equal(h.el('repair-radio').disabled,true);h.api.showRepairConfirmation();await h.api.repairSavedUpload();assert.equal(h.posts().length,0);
  }
 });
 await check('A different connection’s pending upload is not repaired by the selected external programmer',async()=>{
  const h=repairHarness('rx');h.api.role('programmer');h.api.renderControls();
  assert.equal(h.el('repair-radio').hidden,true);h.api.showRepairConfirmation();await h.api.repairSavedUpload();assert.equal(h.posts().length,0);
 });
 await check('Failed status refresh, cancellation and native dialog close do not repair or clear a saved draft',async()=>{
  for(const mode of['failure','cancel','native-close']){
   const h=repairHarness();const draft=h.draft();draft.memories[1].name='UNSAVED';h.api.draft(draft);h.api.showRepairConfirmation();
   h.onStatus=()=>{if(mode==='failure')throw Error('offline');if(mode==='cancel')h.api.closeRepairConfirmation();else h.el('repair-dialog').close();};
   await h.api.repairSavedUpload();assert.equal(h.posts().length,0);assert.deepEqual(h.draft(),draft);
  }
 });
 await check('A repair status request in flight prevents a duplicate confirmation and other editor operations',async()=>{
  const h=repairHarness();h.api.showRepairConfirmation();let started,release;
  const waiting=new Promise(resolve=>{release=resolve;});const ready=new Promise(resolve=>{started=resolve;});
  h.onStatus=()=>{started();return waiting;};const first=h.api.repairSavedUpload();await ready;
  assert.equal(h.el('read-radio').disabled,true);assert.equal(h.el('repair-confirm').disabled,true);
  await h.api.repairSavedUpload();await h.api.startOperation('read');assert.equal(h.posts().length,0);
  release();await first;assert.equal(h.posts().length,1);
 });
 await check('Repair dialog describes physical-radio confirmation, bounded backups and verification before audio resumes',()=>{
  const html=fs.readFileSync(path.join(root,'dashboard/static/radio-editor.html'),'utf8');
  assert.match(html,/id="repair-impact">Confirm the same physical radio.*fresh backup.*only affected pages.*five-set limit.*paused.*every unfinished upload verify/);
  assert.match(html,/id="repair-draft-warning" hidden>This operation does not include your unsaved draft/);
 });
 await check('Hidden editor documents bounded backups, automatic reconnect and conditional recovery',()=>{
  const html=fs.readFileSync(path.join(root,'dashboard/static/radio-editor.html'),'utf8');
  for(const phrase of['noindex,nofollow','five most recent sets per connection','does not show the radio’s current frequency','The helper reconnects automatically between operations','Review before writing'])assert.ok(html.includes(phrase),phrase);
  assert.doesNotMatch(html,/Power (?:it|the radio) off and on|before each read|power.cycle/i);
  assert.match(html,/If the radio does not reconnect after a failed task, restart it/);
  for(const id of['read-dialog','review-dialog','verify-dialog','repair-dialog']){
   const dialog=html.match(new RegExp(`<dialog[^>]+id="${id}"[\\s\\S]*?<\\/dialog>`))[0];
   assert.match(dialog,/Keep (?:the radio connected and|the radio and Pi) powered|Keep the cable connected and leave the radio and Pi powered/);
  }
  assert.ok(!fs.readFileSync(path.join(root,'dashboard/static/index.html'),'utf8').includes('radio-editor'));assert.match(original,/\/api\/radio-programming\/status/);
 });
 await check('External profile choices are explicit and absent from the two fixed box connections',()=>{
  const h=profileHarness();assert.equal(h.el('radio-model-label').hidden,false);
  assert.deepEqual(h.el('radio-model').children.map(item=>item.value),['5RX','UV5R']);
  h.api.role('rx');h.api.renderControls();assert.equal(h.el('radio-model-label').hidden,true);
  h.api.selectModel('UV5R');assert.equal(h.api.selectedModel(),'5RX');assert.equal(h.posts().length,0);
  const legacy=presetHarness('programmer');legacy.api.renderControls();assert.equal(legacy.el('radio-model').disabled,true);
  legacy.api.selectModel('UV5R');assert.equal(legacy.api.selectedModel(),'5RX');
 });
 await check('A clean external profile change clears its snapshot and requires a new read',()=>{
  const h=profileHarness();h.api.selectModel('UV5R');
  assert.equal(h.api.selectedModel(),'UV5R');assert.equal(h.api.getSnapshot(),null);assert.equal(h.api.canEdit(),false);
  assert.equal(h.calls.length,0);assert.equal(h.posts().length,0);assert.equal(h.el('radio-model').value,'UV5R');
 });
 await check('Polling cannot resurrect a stored snapshot after an explicit profile switch',async()=>{
  const h=profileHarness();h.api.selectModel('UV5R');const next={...clone(snapshot),role:'programmer',model:'UV5R',base_id:'b'.repeat(32)};
  h.setSnapshot(next);Object.assign(h.status.radio_programming,{action:'editor-read',pending:false,success:true,role:'programmer',request_id:next.base_id});
  await h.api.refresh();assert.equal(h.api.getSnapshot(),null);assert.equal(h.calls.filter(call=>call.url.startsWith('/api/radio-editor?')).length,0);
  h.el('read-dialog').showModal();await h.api.startOperation('read');
  Object.assign(h.status.radio_programming,{pending:false,success:true,request_id:'c'.repeat(32)});next.base_id='c'.repeat(32);h.setSnapshot(next);
  await h.api.refresh();assert.equal(h.api.getSnapshot().model,'UV5R');assert.equal(h.api.getSnapshot().base_id,next.base_id);
 });
 await check('An external profile change requires confirmation before discarding a draft',()=>{
  const h=profileHarness();const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);
  h.api.selectModel('UV5R');assert.equal(h.el('discard-dialog').open,true);assert.equal(h.api.selectedModel(),'5RX');
  assert.match(h.el('discard-description').textContent,/UV-5R.*snapshot will be cleared.*Read the connected radio/);
  h.api.closeDialogs();assert.deepEqual(h.draft(),draft);assert.equal(h.api.selectedModel(),'5RX');
  h.api.selectModel('UV5R');h.api.confirmDiscard();assert.equal(h.api.selectedModel(),'UV5R');
  assert.equal(h.api.getSnapshot(),null);assert.equal(h.posts().length,0);
 });
 await check('Profile-switch confirmation cannot discard a different snapshot or admin session',()=>{
  for(const mutate of[h=>h.api.setSession({configured:true,authenticated:true,csrf_token:'new-token'}),h=>h.api.loadSnapshot({...clone(snapshot),role:'programmer',base_id:'d'.repeat(32)})]){
   const h=profileHarness();const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);h.api.selectModel('UV5R');mutate(h);
   h.api.confirmDiscard();assert.equal(h.api.selectedModel(),'5RX');assert.equal(h.posts().length,0);
  }
 });
 await check('Snapshot loading rejects a profile mismatch or an unknown model',()=>{
  const h=profileHarness();for(const value of['UV5R','OTHER'])assert.equal(h.api.loadSnapshot({...clone(snapshot),role:'programmer',model:value}),false);
  assert.equal(h.api.getSnapshot().model,'5RX');h.api.role('rx');assert.equal(h.api.loadSnapshot({...clone(snapshot),model:'UV5R'}),false);
 });
 await check('External snapshot queries pin the selected model while ordinary box queries stay unchanged',async()=>{
  const h=profileHarness('UV5R');await h.api.fetchSnapshot(true);assert.equal(h.calls[0].url,'/api/radio-editor?role=programmer&model=UV5R');
  assert.match(h.el('snapshot-meta').textContent,/UV-5R \/ UV5R.*BFB297/);
  const rx=harness();await rx.api.fetchSnapshot(true);assert.equal(rx.calls[0].url,'/api/radio-editor?role=rx');
 });
 await check('A late snapshot for another external profile cannot replace the selected snapshot',async()=>{
  const h=profileHarness();const hold=holdSnapshot(h);const pending=h.api.fetchSnapshot(true);await hold.started;
  h.api.model('UV5R');const next={...clone(snapshot),role:'programmer',model:'UV5R',base_id:'d'.repeat(32)};
  h.api.loadSnapshot(next);hold.release();await pending;
  assert.equal(h.api.getSnapshot().model,'UV5R');assert.equal(h.api.getSnapshot().base_id,next.base_id);assert.equal(h.posts().length,0);
 });
 await check('External reads include the selected profile while verify uses only the saved pending job',async()=>{
  const h=profileHarness('UV5R');h.el('read-dialog').showModal();await h.api.startOperation('read');
  assert.deepEqual(JSON.parse(h.posts()[0].options.body),{role:'programmer',model:'UV5R'});
  const pending=profileHarness('UV5R',true);pending.el('verify-dialog').showModal();await pending.api.startOperation('verify');
  assert.deepEqual(JSON.parse(pending.posts()[0].options.body),{role:'programmer',action:'verify'});
 });
 await check('A changed external model during the final read status check sends no radio request',async()=>{
  const h=profileHarness();h.el('read-dialog').showModal();h.onStatus=()=>{h.api.model('UV5R');};await h.api.startOperation('read');
  assert.equal(h.posts().length,0);
 });
 await check('Unfinished external uploads select and lock their saved profile',()=>{
  const h=profileHarness();const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);h.api.reviewDraft();
  Object.assign(h.status.radio_programming.roles.programmer,{model:'UV5R',verification_required:true,program_kind:'editor',pending_id:'e'.repeat(32)});
  h.api.status(clone(h.status));h.api.renderTask();assert.equal(h.api.selectedModel(),'UV5R');assert.equal(h.el('radio-model').disabled,true);
  assert.equal(h.api.getSnapshot(),null);assert.equal(h.el('review-dialog').open,false);
  h.api.selectModel('5RX');assert.equal(h.api.selectedModel(),'UV5R');assert.equal(h.posts().length,0);
 });
 await check('UV5R recovery describes its saved profile and never sends a model override',async()=>{
  const h=profileHarness('UV5R',true);h.api.showRepairConfirmation();assert.match(h.el('repair-description').textContent,/configured UV-5R \/ UV5R profile/);
  await h.api.repairSavedUpload();assert.equal(h.posts().length,1);assert.deepEqual(JSON.parse(h.posts()[0].options.body),{role:'programmer',action:'repair-upload',pending_id:'e'.repeat(32)});
 });
 await check('A saved-upload profile change invalidates a reviewed recovery',async()=>{
  const h=profileHarness('UV5R',true);h.api.showRepairConfirmation();h.onStatus=()=>{h.status.radio_programming.roles.programmer.model='5RX';};
  await h.api.repairSavedUpload();assert.equal(h.posts().length,0);
 });
 await check('UV5R settings are driven by its returned capability subset',()=>{
  const h=profileHarness('UV5R');assert.equal(h.el('radio-settings').children.length,2);assert.equal(h.el('setting-settings-squelch').type,'number');
  const draft=h.draft();draft.settings['settings.squelch']=10;assert.match(h.api.validationErrors(h.api.getSnapshot(),draft).join(' '),/Squelch/);
  draft.settings['settings.squelch']=2;assert.equal(h.api.validationErrors(h.api.getSnapshot(),draft).length,0);
  draft.settings['settings.pttid']='Off';assert.match(h.api.validationErrors(h.api.getSnapshot(),draft).join(' '),/settings.pttid/);
 });
 await check('UV5R writes bind the reviewed profile privately and send no model field',async()=>{
  const h=profileHarness('UV5R');const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);h.api.reviewDraft();
  assert.equal(h.api.getProposal().model,'UV5R');await h.api.writeDraft();assert.equal(h.posts().length,1);
  const payload=JSON.parse(h.posts()[0].options.body);assert.equal(payload.role,'programmer');assert.equal(payload.base_id,snapshot.base_id);assert.equal('model' in payload,false);
 });
 await check('A model change invalidates memory, preset and write proposal contexts',async()=>{
  for(const action of['add','delete','preset','write']){
   const h=profileHarness();
   if(action==='add')h.api.openAddChannel();
   if(action==='delete')h.api.requestDeleteChannel(1);
   if(action==='preset')selectPreset(h,'private','rpt1',0);
   if(action==='write'){const draft=h.draft();draft.memories[1].name='LOCAL';h.api.draft(draft);h.api.reviewDraft();}
   const before=h.draft();h.api.model('UV5R');
   if(action==='add')h.api.confirmAddChannel();if(action==='delete')h.api.confirmDeleteChannel();if(action==='preset')h.api.confirmRepeaterPreset();if(action==='write')await h.api.writeDraft();
   assert.deepEqual(h.draft(),before);assert.equal(h.posts().length,0);
  }
 });
 console.log(`${checks.length} radio editor checks passed.`);
})().catch(error=>{console.error(error);process.exitCode=1;});
