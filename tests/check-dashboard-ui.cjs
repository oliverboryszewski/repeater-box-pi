const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../dashboard/static/app.js'),'utf8').replace(/  selectTab\(location\.hash\.slice\(1\)\);[\s\S]*?\n\}\)\(\);\s*$/, `  globalThis.testApi = {
  setup(status, auth) { latestStatus = status; session = auth; sessionKnown = true; renderWifiVisibility(status.wifi_visibility); renderSoftwareUpdates(status.software_updates); renderRadioProgramming(status.radio_programming); renderDtmf(); },
  session(auth, known=true) { session = auth; sessionKnown = known; renderAdmin(); },
  status(status) { latestStatus = status; renderWifiVisibility(status.wifi_visibility); renderSoftwareUpdates(status.software_updates); renderRadioProgramming(status.radio_programming); renderDtmf(); },
  settings(settings) { renderSettings(settings); },
  live(live) { renderLive(live); },
  channelPair(status) { renderChannelPair(status); },
  activity(value) { renderRadioActivity(value); },
  startActivity() { return startRadioActivity(); },
  stopActivity() { stopRadioActivity(); },
  messagesProgress() { return refreshRadioMessages(); },
  progress() { return refreshRadioProgress(); },
  refresh() { return refreshAfterMutation(); },
  };\n})();`);
function harness({preview=false, hidden=false, updates={}, radios={}, stationIdentification={},messages={}}={}) {
  let focused = null, onStatus = null, onActivity = null, failActivity = false, failRefresh = false, pairError = null, messageError = null, stationIdError = null, stationIdResult = null, softwareRequests = 1;
  const elements = new Map(), calls=[], timers=new Map();let timerId=0;
  class Element {
    constructor(id='') {this.id=id;this.listeners={};this.attrs={};this.dataset={};this.open=false;this.classList={add(){},remove(){},toggle(){}};this.value='';this.children=[];this.firstChild={nodeType:3,nodeValue:''};}
    addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
    async dispatch(type,extra={}){const event={type,target:this,...extra,preventDefault(){this.defaultPrevented=true;}};for(const fn of this.listeners[type]??[]) await fn(event);return event;}
    setAttribute(k,v){this.attrs[k]=v;} removeAttribute(k){delete this.attrs[k];if(k==='src')delete this.src;}
    replaceChildren(...nodes){this.children=nodes;} append(...nodes){this.children.push(...nodes);} add(node){this.children.push(node);}
    focus(){focused=this.id;} pause(){} load(){}
    setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}
    showModal(){assert.equal(this.open,false);this.open=true;}
    close(){this.open=false;for(const fn of this.listeners.close??[])fn({});}
  }
  const el=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
  const auth={configured:true,authenticated:!preview,csrf_token:preview?null:'token-1'};
  const plan={rx:{memory:1,name:'BOX-RX1',frequency_mhz:'145.300000',receive_tone_hz:88.5,transmit_tone_hz:null,transmit:false,power:'High',mode:'NFM'},tx:{memory:1,name:'BOX-TX1',frequency_mhz:'433.500000',receive_tone_hz:null,transmit_tone_hz:88.5,transmit:true,power:'High',mode:'NFM'}};
  let status={dtmf_control:{available:true,configured:true,bridge_ready:true,enabled:false,status_announcements:true,code_change:{available:true,pending:false,request_id:null,success:null,message:null}},radio_programming:{available:!preview,configured:!preview,catalog_configured:true,catalog_digest:'c'.repeat(64),preview,pending:false,request_id:'radio-1',plan,roles:{rx:{inspected:true,compatible:true,firmware:'HN5RV011',identity:'known-rx',programmed:true,verified:true},tx:{inspected:true,compatible:true,firmware:'HN5RV011',identity:'known-tx',programmed:true,verified:true}},...radios},version:'0.2',wifi_visibility:{available:!preview,preview,pending:false,profile_hidden:hidden,ssid:'Repeater-Box',request_id:'req-1'},admin:{configured:true,authenticated:!preview},network:{ssid:'Repeater-Box'},settings:{},recorder:{},software_updates:{available:!preview,preview,pending:false,current_version:'0.2.2',current_commit:'a'.repeat(40),latest_version:'0.2.3',latest_commit:'b'.repeat(40),update_available:true,action:'check',success:true,request_id:'updates-1',checked_at:'2026-10-19T19:00:00+00:00',...updates}};
  status.radio_messages={available:!preview,can_upload:!preview,can_broadcast:!preview,reason:'',limits:{max_upload_bytes:8388608,max_duration_seconds:30,allowed_extensions:['wav','mp3','m4a'],max_messages:10},messages:[],upload_pending:false,job:null,invocation_id:'b'.repeat(32),pair_id:'default',catalog_digest:'c'.repeat(64),...messages};
  status.station_identification={available:!preview,enabled:true,...stationIdentification};
  const ctx={console, URL, AbortController, Node:{TEXT_NODE:3}, Option:Element, document:{getElementById:el,querySelector:el,createElement:()=>new Element(),addEventListener(){},hidden:false},location:{hash:'#settings',origin:'http://10.42.0.1'},history:{replaceState(){}},window:{addEventListener(){}},setTimeout:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);},setInterval(){},fetch:async(path,options={})=>{
    calls.push({path,options});
    if(path==='/api/status'){
      if(onStatus) await onStatus();
      if(failRefresh)throw new Error('offline');
      return{ok:true,json:async()=>structuredClone(status)};
    }
    if(path==='/api/activity'){if(onActivity)await onActivity();if(failActivity)throw new Error('offline');return{ok:true,json:async()=>structuredClone(status.radio_activity||{available:false,receiving:null,transmitting:null})};}
    if(path==='/api/messages')return{ok:true,json:async()=>({radio_messages:structuredClone(status.radio_messages)})};
    if(path.startsWith('/api/messages/')){
      if(messageError)throw new Error(messageError);
      if(path.endsWith('/upload')){
        const entry=messageEntry('a'.repeat(32),decodeURIComponent(options.headers['X-File-Name']));
        status.radio_messages.messages.push(entry);
        return{ok:true,json:async()=>({radio_messages:structuredClone(status.radio_messages),message:structuredClone(entry)})};
      }
      const body=JSON.parse(options.body);
      if(path.endsWith('/delete'))status.radio_messages.messages=status.radio_messages.messages.filter(item=>item.id!==body.id);
      else if(path.endsWith('/broadcast'))status.radio_messages.job={request_id:'e'.repeat(32),message_id:body.id,phase:'queued',pending:true,success:null,message:'Waiting for the next clear transmit window.'};
      else throw new Error(`Unexpected message route ${path}`);
      return{ok:true,json:async()=>({radio_messages:structuredClone(status.radio_messages)})};
    }
    if(path==='/api/wifi-visibility'){
      const body=JSON.parse(options.body);status.wifi_visibility={...status.wifi_visibility,profile_hidden:body.hidden,request_id:'req-2',success:true,requested_hidden:body.hidden};
      return{ok:true,json:async()=>({wifi_visibility:structuredClone(status.wifi_visibility)})};
    }
    if(path==='/api/radio-programming'){
      const body=JSON.parse(options.body);status.radio_programming={...status.radio_programming,pending:true,role:body.role,action:body.action,request_id:'radio-2',success:null};
      return{ok:true,json:async()=>({radio_programming:structuredClone(status.radio_programming)})};
    }
    if(path==='/api/radio-pair'){
      if(pairError)return{ok:false,status:pairError.status||400,json:async()=>({error:pairError.message})};
      const body=JSON.parse(options.body);
      const preset=status.radio_programming.pair_switching.presets.find(item=>item.id===body.pair_id);
      status.radio_programming={...status.radio_programming,pending:true,action:'change-pair',phase:'preflight',request_id:'f'.repeat(32),success:null,
        pair:{pair_id:body.pair_id,request_id:'f'.repeat(32),phase:'prepared',stored_verified:false,activation_required:false,verification_required:false,
          roles:{rx:{written:false,verified:false},tx:{written:false,verified:false}},
          plan:{name:preset.name,rx_hz:preset.input_hz,tx_hz:preset.output_hz,tone_hz:88.5,mode:'NFM',power:'High'}}};
      delete status.radio_programming.role;
      return{ok:true,json:async()=>({radio_programming:structuredClone(status.radio_programming)})};
    }
    if(path==='/api/admin/logout'){status.admin.authenticated=false;return{ok:true,json:async()=>({configured:true,authenticated:false,csrf_token:null})};}
    if(path==='/api/station-identification'){
      if(stationIdError)return{ok:false,status:409,json:async()=>({error:stationIdError})};
      const body=JSON.parse(options.body);status.station_identification={...status.station_identification,enabled:body.enabled};
      return{ok:true,json:async()=>stationIdResult ?? ({station_identification:structuredClone(status.station_identification)})};
    }
    if(path==='/api/dtmf-control'){const body=JSON.parse(options.body);status.dtmf_control={...status.dtmf_control,...body};return{ok:true,json:async()=>({dtmf_control:structuredClone(status.dtmf_control)})};}
    if(path==='/api/dtmf-password'){status.dtmf_control.code_change={available:true,pending:true,request_id:'9'.repeat(32),success:null,message:'Changing the private DTMF password.'};return{ok:true,json:async()=>({code_change:structuredClone(status.dtmf_control.code_change)})};}
    if(path==='/api/settings'){
      status.settings=JSON.parse(options.body);
      return{ok:true,json:async()=>({settings:structuredClone(status.settings)})};
    }
    if(path==='/api/software-updates/check'||path==='/api/software-updates/install'){
      status.software_updates={...status.software_updates,pending:true,action:path.endsWith('/install')?'install':'check',request_id:`updates-${++softwareRequests}`,success:null};
      return{ok:true,json:async()=>({software_updates:structuredClone(status.software_updates)})};
    }
    throw new Error(`unexpected fetch ${path}`);
  }};
  vm.createContext(ctx);vm.runInContext(source,ctx);ctx.testApi.setup(structuredClone(status),structuredClone(auth));
  return{ctx,el,calls,timers,set onActivity(fn){onActivity=fn;},set failActivity(value){failActivity=value;},get focused(){return focused;},get status(){return status;},set onStatus(fn){onStatus=fn;},set failRefresh(v){failRefresh=v;},set pairError(value){pairError=value;},set messageError(value){messageError=value;},set stationIdError(value){stationIdError=value;},set stationIdResult(value){stationIdResult=value;},posts:()=>calls.filter(x=>x.options.method==='POST')};
}
const checks=[];
async function check(name,fn){try{await fn();checks.push(name);}catch(error){throw new Error(name,{cause:error});}}
function pairHarness(options={}) {
 const presets=[
  {id:'default',label:'Original pair',index:1,input_hz:145300000,output_hz:433500000,rx_name:'BOX-RX1',tx_name:'BOX-TX1',name:'BOX-RX1 / BOX-TX1',tone_hz:88.5},
  {id:'rpt1',label:'Repeater pair 1',index:2,input_hz:145330000,output_hz:433525000,rx_name:'BOX-RX2',tx_name:'BOX-TX2',name:'BOX-RX2 / BOX-TX2',tone_hz:88.5},
  {id:'rpt3',label:'Lab pair 4',index:4,input_hz:145390000,output_hz:433575000,rx_name:'BOX-RX4',tx_name:'BOX-TX4',name:'BOX-RX4 / BOX-TX4',tone_hz:88.5}
 ];
 const pair={pair_id:'default',request_id:'d'.repeat(32),phase:'configured',stored_verified:true,activation_required:false,verification_required:false,
  roles:{rx:{written:true,verified:true},tx:{written:true,verified:true}},
  plan:{name:'BOX-RX1 / BOX-TX1',rx_hz:145300000,tx_hz:433500000,tone_hz:88.5,mode:'NFM',power:'High'}};
 return harness({...options,radios:{pair_switching:{enabled:true,presets,message:'Qualified for this radio pair.'},pair,...options.radios}});
}
async function freshUpdateCheck(h) {
 await h.el('software-check').dispatch('click');
 assert.equal(h.posts().at(-1)?.path,'/api/software-updates/check');
 Object.assign(h.status.software_updates,{pending:false,action:'check',success:true});
 h.ctx.testApi.status(structuredClone(h.status));
 h.calls.length=0;
 return h;
}
async function openPair(h,pairId='rpt1') {
 h.el('radio-pair-select').value=pairId;
 await h.el('radio-pair-select').dispatch('change');
 await h.el('radio-pair-change').dispatch('click');
}
async function confirmPair(h,password='secret') {
 h.el('radio-pair-confirm-password').value=password;
 await h.el('radio-pair-confirm-password').dispatch('input');
 await h.el('radio-pair-confirm-apply').dispatch('click');
}
function repairHarness(role='rx',kind='editor',options={}) {
  const h=harness(options);
  Object.assign(h.status.radio_programming.roles[role],{program_kind:kind,verification_required:true,verified:false,pending_id:'e'.repeat(32)});
  h.ctx.testApi.status(structuredClone(h.status));return h;
}
function messageEntry(id='1'.repeat(32),name='Field message.wav') {
 return{id,sha256:'d'.repeat(64),name,duration_seconds:12.4,bytes:1190444,created_at:'2026-10-29T19:00:00+00:00',preview_url:`/api/messages/${id}/audio`};
}
function messageHarness(options={}) {
 return pairHarness({...options,messages:{messages:[messageEntry()],...options.messages}});
}
async function selectMessage(h,id='1'.repeat(32)) {
 h.el('radio-message-select').value=id;await h.el('radio-message-select').dispatch('change');
}
function dashboardMarkup() {
  const html=fs.readFileSync(path.resolve(__dirname,'../dashboard/static/index.html'),'utf8');
  const root={tag:'#document',attrs:{},parent:null}, stack=[root], ids=new Map(), nodes=[];
  const voidTags=new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
  for(const match of html.matchAll(/<!--[\s\S]*?-->|<![^>]*>|<\/?([a-z][\w-]*)\b([^>]*)>/gi)){
    if(!match[1])continue;
    const tag=match[1].toLowerCase();
    if(match[0].startsWith('</')){assert.equal(stack.pop()?.tag,tag,`Balanced ${tag} element`);continue;}
    const attrs={};for(const attr of match[2].matchAll(/([^\s=]+)(?:\s*=\s*"([^"]*)"|\s*=\s*'([^']*)'|\s*=\s*([^\s"'=<>`]+))?/g))attrs[attr[1]]=attr[2]??attr[3]??attr[4]??'';
    if(tag==='form')assert.equal(stack.some(node=>node.tag==='form'),false,'Forms must not nest');
    const node={tag,attrs,parent:stack.at(-1)};nodes.push(node);
    if(attrs.id){assert.equal(ids.has(attrs.id),false,`Unique id ${attrs.id}`);ids.set(attrs.id,node);}
    if(!voidTags.has(tag)&&!match[0].endsWith('/>'))stack.push(node);
  }
  assert.equal(stack.length,1,'All HTML elements close');
  const inside=(id,ancestor)=>{let node=ids.get(id);assert.ok(node,id);while(node){if(node.attrs.id===ancestor)return true;node=node.parent;}return false;};
  return{html,ids,nodes,inside};
}
(async()=>{
  await check('Overview uses the selected verified pair rather than an old default template',async()=>{
    const h=pairHarness();Object.assign(h.status.radio_programming.pair,{pair_id:'rpt1',plan:{name:'BOX-RX2 / BOX-TX2',rx_hz:145330000,tx_hz:433525000,tone_hz:88.5,power:'High'}});
    h.status.setup={rf_enabled:true,radio_plan:{verified:true,rx_mhz:'145.300000',tx_mhz:'433.500000',tone_hz:88.5,power:'High',label:'Pair 2',rx_name:'BOX-RX2',tx_name:'BOX-TX2'}};
    h.ctx.testApi.channelPair(h.status);assert.equal(h.el('channels-title').textContent,'Active channel pair');
    assert.equal(h.el('rx-frequency').textContent,'145.330000');assert.equal(h.el('tx-frequency').textContent,'433.525000');
    assert.equal(h.el('tone-label').textContent,'Tone');assert.equal(h.el('power-label').textContent,'TX power');assert.equal(h.el('tone-frequency').textContent,'88.5 Hz');
    assert.equal(h.el('channel-pair-caption').textContent,'Pair 2 · BOX-RX2 / BOX-TX2');assert.match(h.el('rf-notice').textContent,/last verified pair/);assert.doesNotMatch(h.el('rf-notice').textContent,/not commissioned/);
  });
  await check('Public overview can show the verified setup pair without private admin programming details',async()=>{
    const h=harness();const status={setup:{rf_enabled:true,radio_plan:{verified:true,rx_mhz:'145.330000',tx_mhz:'433.525000',tone_hz:null,power:'High',label:'Pair 2',rx_name:'BOX-RX2',tx_name:'BOX-TX2'}},radio_programming:{admin_required:true}};
    h.ctx.testApi.channelPair(status);assert.equal(h.el('channels-title').textContent,'Active channel pair');assert.equal(h.el('tone-frequency').textContent,'Off');assert.equal(h.el('rx-frequency').textContent,'145.330000');assert.match(h.el('channel-pair-caption').textContent,/Pair 2/);
  });
  for(const [name,change]of[
    ['new program pending',s=>{s.radio_programming.pending=true;}],
    ['unfinished verification',s=>{s.radio_programming.roles.rx.verification_required=true;}],
    ['pair verification pending',s=>{s.radio_programming.pair.verification_required=true;}],
    ['activation not confirmed',s=>{s.radio_programming.pair.activation_required=true;}],
    ['catalog changed',s=>{s.radio_programming.pair.catalog_changed=true;}],
    ['backend marks plan unverified',s=>{s.setup.radio_plan.verified=false;}]
  ])await check(`${name} cannot claim an active verified overview pair`,async()=>{
    const h=pairHarness();h.status.setup={rf_enabled:true,radio_plan:{verified:true,rx_mhz:'145.300000',tx_mhz:'433.500000'}};change(h.status);h.ctx.testApi.channelPair(h.status);
    assert.doesNotMatch(h.el('channels-title').textContent,/Active/);assert.equal(h.el('tone-label').textContent,'Planned tone');assert.equal(h.el('channel-pair-caption').hidden,true);assert.doesNotMatch(h.el('rf-notice').textContent,/not commissioned/);
  });
  await check('Activity dots represent independent receiver squelch and transmit-command states accessibly',async()=>{
    const h=harness();h.ctx.testApi.activity({available:true,receiving:true,transmitting:false});
    assert.equal(h.el('rx-activity').className,'radio-activity-dot activity-on');assert.equal(h.el('tx-activity').className,'radio-activity-dot activity-off');
    assert.equal(h.el('rx-activity').attrs['aria-label'],'Receiver squelch open');assert.equal(h.el('tx-activity').attrs['aria-label'],'Transmit command idle');
    h.ctx.testApi.activity({available:true,receiving:false,transmitting:true});assert.equal(h.el('rx-activity').className,'radio-activity-dot activity-off');assert.equal(h.el('tx-activity').attrs['aria-label'],'Transmit command active');
    h.ctx.testApi.activity({available:false,receiving:true,transmitting:true});for(const role of['rx','tx']){assert.equal(h.el(`${role}-activity`).className,'radio-activity-dot activity-unknown');assert.match(h.el(`${role}-activity`).attrs['aria-label'],/unavailable/);}
    h.ctx.testApi.activity({available:true,receiving:null,transmitting:false});assert.equal(h.el('rx-activity').className,'radio-activity-dot activity-unknown');assert.equal(h.el('tx-activity').className,'radio-activity-dot activity-off');
  });
  await check('Activity refresh uses a light endpoint and one 500 ms timer; stop clears it and both indicators',async()=>{
    const h=harness();h.status.radio_activity={available:true,receiving:true,transmitting:false};await h.ctx.testApi.startActivity();
    assert.equal(h.calls.length,1);assert.equal(h.calls[0].path,'/api/activity');assert.equal(h.calls[0].options.cache,'no-store');assert.equal(h.posts().length,0);
    const activityTimers=[...h.timers.values()].filter(timer=>timer.ms===500);assert.equal(activityTimers.length,1);assert.equal(h.el('rx-activity').className,'radio-activity-dot activity-on');
    await h.ctx.testApi.startActivity();assert.equal(h.calls.length,1,'Starting twice never duplicates the loop');
    h.ctx.testApi.stopActivity();assert.equal(h.timers.size,0);assert.equal(h.el('rx-activity').className,'radio-activity-dot activity-unknown');assert.equal(h.el('tx-activity').className,'radio-activity-dot activity-unknown');
  });
  await check('Activity errors clear stale ON states and keep a single retry timer',async()=>{
    const h=harness();h.status.radio_activity={available:true,receiving:true,transmitting:true};await h.ctx.testApi.startActivity();
    h.failActivity=true;const [id,timer]=[...h.timers].find(([,item])=>item.ms===500);h.timers.delete(id);await timer.fn();await Promise.resolve();
    assert.equal(h.el('rx-activity').className,'radio-activity-dot activity-unknown');assert.equal(h.el('tx-activity').className,'radio-activity-dot activity-unknown');assert.equal([...h.timers.values()].filter(timer=>timer.ms===500).length,1);h.ctx.testApi.stopActivity();
  });
  await check('Hidden pages do not poll, and a stopped in-flight activity response cannot restore a stale ON dot',async()=>{
    const h=harness();h.ctx.document.hidden=true;await h.ctx.testApi.startActivity();assert.equal(h.calls.length,0);assert.equal(h.timers.size,0);
    h.ctx.document.hidden=false;let release;h.onActivity=()=>new Promise(resolve=>{release=resolve;});h.status.radio_activity={available:true,receiving:true,transmitting:true};
    const request=h.ctx.testApi.startActivity();await h.ctx.testApi.startActivity();assert.equal(h.calls.length,1);h.ctx.testApi.stopActivity();release();await request;
    assert.equal(h.el('tx-activity').className,'radio-activity-dot activity-unknown');assert.equal(h.timers.size,0);assert.equal(h.calls[0].options.signal.aborted,true);
  });
  await check('Radio messages live in Daily operation with a confirmation dialog and no extra password field',async()=>{
    const {html,ids,inside}=dashboardMarkup();
    assert.equal(inside('radio-messages-card','admin-daily'),true);
    assert.equal(inside('radio-message-confirm-dialog','admin-options'),false);
    assert.equal(ids.get('radio-message-file').attrs.accept,'.wav,.mp3,.m4a');
    assert.equal(ids.get('radio-message-audio').attrs.preload,'none');
    const dialog=html.slice(html.indexOf('<dialog class="confirm-dialog" id="radio-message-confirm-dialog"'),html.indexOf('</dialog>',html.indexOf('id="radio-message-confirm-dialog"')));
    assert.doesNotMatch(dialog,/type="password"|setInterval|repeat/i);
  });
  await check('Message controls, names, draft, playback and confirmation clear on sign-out',async()=>{
    const h=messageHarness();await selectMessage(h);h.el('radio-message-file').value='draft.wav';
    await h.el('radio-message-broadcast').dispatch('click');assert.equal(h.el('radio-message-confirm-dialog').open,true);
    h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});
    assert.equal(h.el('radio-messages-card').hidden,true);assert.equal(h.el('radio-message-confirm-dialog').open,false);
    assert.equal(h.el('radio-message-name').textContent,'');assert.equal(h.el('radio-message-audio').src,undefined);assert.equal(h.el('radio-message-file').value,'');
    await h.el('radio-message-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
  });
  await check('Preview and upload drafts survive ordinary status polling without transmitting',async()=>{
    const h=messageHarness();await selectMessage(h);
    const options=h.el('radio-message-select').children, audio=h.el('radio-message-audio'), file={name:'Draft voice.mp3',size:8000};
    h.el('radio-message-file').files=[file];await h.el('radio-message-file').dispatch('change');
    h.ctx.testApi.status(structuredClone(h.status));
    assert.equal(h.el('radio-message-select').children,options);assert.equal(h.el('radio-message-select').value,'1'.repeat(32));
    assert.equal(h.el('radio-message-file').files[0],file);assert.match(h.el('radio-message-file-detail').textContent,/Draft voice/);
    assert.equal(audio.src,'http://10.42.0.1/api/messages/'+ '1'.repeat(32)+'/audio');
    await audio.dispatch('play');assert.equal(h.posts().length,0);
  });
  for(const url of['https://example.org/audio','//example.org/audio','/api/messages/'+ '2'.repeat(32)+'/audio','/api/messages/'+ '1'.repeat(32)+'/audio?token=leak','/api/messages/'+ '1'.repeat(32)+'/audio#x','/api/messages/../private/audio'])
    await check(`Unsafe message preview URL is never loaded: ${url}`,async()=>{
      const h=messageHarness({messages:{messages:[{...messageEntry(),preview_url:url}]}});await selectMessage(h);
      assert.equal(h.el('radio-message-audio').src,undefined);assert.equal(h.el('radio-message-audio').hidden,true);assert.equal(h.posts().length,0);
    });
  await check('Uploaded names render as plain text in selection and confirmation',async()=>{
    const name='<img src=x onerror=alert(1)>.wav',h=messageHarness({messages:{messages:[messageEntry('1'.repeat(32),name)]}});await selectMessage(h);
    assert.equal(h.el('radio-message-name').textContent,name);assert.equal(h.el('radio-message-select').children[1].textContent,name);
    await h.el('radio-message-broadcast').dispatch('click');assert.ok(h.el('radio-message-confirm-description').textContent.startsWith(name));assert.equal(h.posts().length,0);
  });
  await check('Upload sends one raw file with encoded name, CSRF and same-origin credentials then selects the saved clip',async()=>{
    const h=messageHarness(),file={name:'A message #1.M4A',size:1024};h.el('radio-message-file').files=[file];
    await h.el('radio-message-upload-form').dispatch('submit');
    assert.equal(h.posts().length,1);const request=h.posts()[0];assert.equal(request.path,'/api/messages/upload');assert.equal(request.options.body,file);
    assert.equal(request.options.headers['Content-Type'],'application/octet-stream');assert.equal(request.options.headers['X-File-Name'],encodeURIComponent(file.name));assert.equal(request.options.headers['X-CSRF-Token'],'token-1');assert.equal(request.options.credentials,'same-origin');
    assert.equal(h.el('radio-message-select').value,'a'.repeat(32));assert.equal(h.el('radio-message-name').textContent,file.name);assert.equal(h.el('radio-message-file').value,'');
    assert.equal(h.posts().some(request=>request.path.endsWith('/broadcast')),false);
  });
  for(const file of[null,{name:'empty.wav',size:0},{name:'too-big.wav',size:8388609},{name:'not-audio.txt',size:1024}])
    await check(`Invalid message draft is rejected before upload: ${JSON.stringify(file)}`,async()=>{
      const h=messageHarness();h.el('radio-message-file').files=file?[file]:[];await h.el('radio-message-upload-form').dispatch('submit');
      assert.equal(h.posts().length,0);assert.match(h.el('radio-message-message').textContent,/Choose a nonempty WAV/);
    });
  await check('Cancel and Escape never transmit a selected message',async()=>{
    const h=messageHarness();await selectMessage(h);await h.el('radio-message-broadcast').dispatch('click');
    await h.el('radio-message-confirm-cancel').dispatch('click');assert.equal(h.el('radio-message-confirm-dialog').open,false);
    await h.el('radio-message-broadcast').dispatch('click');const event=await h.el('radio-message-confirm-dialog').dispatch('cancel');assert.equal(event.defaultPrevented,true);
    await h.el('radio-message-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
  });
  await check('Explicit broadcast confirmation sends one bounded message and native/pair snapshot with no password',async()=>{
    const h=messageHarness();await selectMessage(h);await h.el('radio-message-broadcast').dispatch('click');assert.equal(h.posts().length,0);
    assert.equal(h.el('radio-message-confirm-apply').disabled,false);await h.el('radio-message-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,1);const request=h.posts()[0];assert.equal(request.path,'/api/messages/broadcast');
    assert.deepEqual(JSON.parse(request.options.body),{id:'1'.repeat(32),sha256:'d'.repeat(64),pair_id:'default',catalog_digest:'c'.repeat(64),invocation_id:'b'.repeat(32)});
    assert.equal(request.options.headers['X-CSRF-Token'],'token-1');assert.equal(h.el('radio-message-confirm-dialog').open,false);
    assert.equal(h.el('radio-message-job').hidden,false);assert.equal(h.el('radio-message-job-progress').hidden,false);
    assert.equal(h.el('radio-message-broadcast').disabled,true);await h.el('radio-message-confirm-apply').dispatch('click');assert.equal(h.posts().length,1);
  });
  for(const [name,change]of[
    ['message hash',h=>{h.status.radio_messages.messages[0].sha256='e'.repeat(64);}],
    ['message removed',h=>{h.status.radio_messages.messages=[];}],
    ['pair',h=>{h.status.radio_messages.pair_id='rpt1';}],
    ['catalog',h=>{h.status.radio_messages.catalog_digest='e'.repeat(64);}],
    ['native invocation',h=>{h.status.radio_messages.invocation_id='e'.repeat(32);}],
    ['live channel busy',h=>{h.status.radio_messages.can_broadcast=false;}],
    ['radio maintenance',h=>{h.status.radio_programming.pending=true;}],
    ['Wi-Fi maintenance',h=>{h.status.wifi_visibility.pending=true;}],
    ['software maintenance',h=>{h.status.software_updates.pending=true;}],
    ['DTMF password maintenance',h=>{h.status.dtmf_control.code_change.pending=true;}],
    ['unfinished radio work',h=>{h.status.radio_programming.roles.rx.verification_required=true;}],
    ['session signed out',h=>{h.status.admin.authenticated=false;}],
    ['CSRF session changed',h=>{h.onStatus=()=>h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'token-2'});}]
  ])await check(`Changed ${name} invalidates message broadcast review before POST`,async()=>{
    const h=messageHarness();await selectMessage(h);await h.el('radio-message-broadcast').dispatch('click');change(h);
    await h.el('radio-message-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('radio-message-confirm-dialog').open,false);
  });
  await check('Failed fresh status and cancellation during status refresh prevent message broadcast',async()=>{
    for(const cancel of[false,true]){
      const h=messageHarness();await selectMessage(h);await h.el('radio-message-broadcast').dispatch('click');
      if(cancel)h.onStatus=()=>h.el('radio-message-confirm-cancel').dispatch('click');else h.failRefresh=true;
      await h.el('radio-message-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
    }
  });
  await check('Selected message change closes an open review and never silently broadcasts another clip',async()=>{
    const h=messageHarness({messages:{messages:[messageEntry(),messageEntry('2'.repeat(32),'Other.wav')]}});await selectMessage(h);await h.el('radio-message-broadcast').dispatch('click');
    await selectMessage(h,'2'.repeat(32));assert.equal(h.el('radio-message-confirm-dialog').open,false);await h.el('radio-message-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
  });
  await check('Uncertain broadcast result stays locked until the Pi reports its job state',async()=>{
    const h=messageHarness();await selectMessage(h);await h.el('radio-message-broadcast').dispatch('click');
    h.messageError='Connection lost';h.onStatus=()=>{if(h.posts().length)h.failRefresh=true;};
    await h.el('radio-message-confirm-apply').dispatch('click');assert.equal(h.posts().length,1);assert.equal(h.el('radio-message-broadcast').disabled,true);
    assert.match(h.el('radio-message-job-phase').textContent,/Confirming/);h.failRefresh=false;h.messageError=null;h.onStatus=null;
    h.status.radio_messages.job={phase:'finished',pending:false,success:true,message:'Message finished.'};await h.ctx.testApi.messagesProgress();
    assert.equal(h.el('radio-message-broadcast').disabled,false);assert.equal(h.el('radio-message-job-progress').hidden,true);assert.match(h.el('radio-message-job-detail').textContent,/finished/);
  });
  await check('Message deletion requires confirmation but works with live transmission unavailable',async()=>{
    const h=messageHarness({messages:{can_broadcast:false,invocation_id:null,reason:'Live mode is stopped.'}});await selectMessage(h);
    assert.equal(h.el('radio-message-broadcast').disabled,true);assert.equal(h.el('radio-message-delete').disabled,false);await h.el('radio-message-delete').dispatch('click');assert.equal(h.posts().length,0);
    await h.el('radio-message-confirm-apply').dispatch('click');assert.equal(h.posts()[0].path,'/api/messages/delete');assert.deepEqual(JSON.parse(h.posts()[0].options.body),{id:'1'.repeat(32),sha256:'d'.repeat(64)});assert.equal(h.el('radio-message-name').textContent,'');
  });
  await check('Pending message tasks lock radio changes, DTMF, identification, recording, Wi-Fi and software controls',async()=>{
    const h=messageHarness({messages:{job:{phase:'playing',pending:true,message:'Message playing.'}}});await selectMessage(h);
    for(const id of['radio-pair-change','radio-inspect-rx','station-id-enabled','dtmf-enabled','recording-toggle','wifi-visibility-fields','settings-fields','software-check','radio-message-upload','radio-message-broadcast','radio-message-delete'])assert.equal(h.el(id).disabled,true,id);
    await h.el('radio-pair-change').dispatch('click');await h.el('dtmf-enabled').dispatch('click');assert.equal(h.posts().length,0);
  });
  await check('Outgoing audio switch defaults on, preserves an unsaved change and saves its boolean with other settings',async()=>{
    const h=harness();h.ctx.testApi.settings({input_enabled:true});assert.equal(h.el('monitor-transmit').checked,true);assert.equal(h.el('monitor-transmit-value').textContent,'On');
    h.el('monitor-transmit').checked=false;await h.el('settings-form').dispatch('input');h.ctx.testApi.settings({monitor_transmit_audio:true});assert.equal(h.el('monitor-transmit').checked,false);assert.equal(h.el('monitor-transmit-value').textContent,'Off');
    await h.el('settings-form').dispatch('submit');assert.equal(JSON.parse(h.posts().at(-1).options.body).monitor_transmit_audio,false);
  });
  await check('Live audio identifies actual TX mix and receiver fallback without claiming off-air monitoring',async()=>{
    const h=harness();h.ctx.testApi.live({configured:true,receiving:true,source:'tx_mix'});assert.match(h.el('live-input-state').textContent,/outgoing included/);
    h.ctx.testApi.live({configured:true,receiving:true,source:'raw_rx'});assert.match(h.el('live-input-state').textContent,/receiver only/);
    const {html}=dashboardMarkup();assert.match(html,/Live radio audio/);assert.match(html,/Each prompt gives you 30 seconds of idle time after it finishes/);
  });
  await check('Admin groups have valid ordered markup, unique IDs, complete ARIA references and no nested forms',async()=>{
    const {html,ids,nodes,inside}=dashboardMarkup();
    let previous=html.indexOf('id="admin-options"');
    for(const name of['daily','audio','network','maintenance','radio-tools']){
      const id=`admin-${name}`,position=html.indexOf(`id="${id}"`);assert.ok(position>previous,id);previous=position;
      assert.equal(ids.get(id).tag,'section');assert.equal(ids.get(id).attrs['aria-labelledby'],`${id}-title`);assert.equal(ids.get(`${id}-title`).tag,'h2');assert.equal(inside(id,'admin-options'),true);
    }
    for(const node of nodes)for(const key of['aria-labelledby','aria-describedby','aria-controls'])for(const id of(node.attrs[key]||'').split(/\s+/).filter(Boolean))assert.equal(ids.has(id),true,`${node.attrs.id||node.tag} ${key}=${id}`);
    for(const id of['radio-pair-select','radio-pair-change','station-id-enabled','dtmf-enabled','dtmf-announcements']){assert.equal(inside(id,'admin-daily'),true,id);assert.equal(inside(id,'radio-tools'),false,id);}
    for(const id of['settings-form','station-name','retention-days','archive-limit','input-enabled','record-startup','keep-indefinitely','recording-threshold','save-settings'])assert.equal(inside(id,'admin-audio'),true,id);
    for(const id of['settings-ssid','settings-address','wifi-broadcast'])assert.equal(inside(id,'admin-network'),true,id);
    for(const id of['software-check','software-install'])assert.equal(inside(id,'admin-maintenance'),true,id);
    for(const id of['radio-inspect-rx','radio-program-tx','radio-unfinished-notice','radio-progress-panel'])assert.equal(inside(id,'radio-tools'),true,id);
    for(const node of nodes.filter(node=>node.tag==='dialog'))assert.equal(inside(node.attrs.id,'admin-options'),false,'Dialogs remain outside collapsible forms and groups');
    assert.equal(ids.get('radio-tools').tag,'details');assert.equal(Object.hasOwn(ids.get('radio-tools').attrs,'open'),false);
  });
  await check('Daily controls remain visible when completed radio tools and password setup are collapsed',async()=>{
    const h=pairHarness();assert.equal(h.el('radio-tools').open,false);assert.match(h.el('radio-tools-summary').textContent,/Both radios verified/);
    for(const id of['radio-pair-picker','station-id-enabled','dtmf-enabled','dtmf-announcements'])assert.equal(h.el(id).hidden,false,id);
    assert.equal(h.el('dtmf-password-tools').open,false);assert.equal(h.el('radio-pair-stages').hidden,true);assert.equal(h.el('radio-pair-progress-help').hidden,true);
    h.el('radio-tools').open=true;h.ctx.testApi.status(h.status);assert.equal(h.el('radio-tools').open,true,'Polling preserves a manually opened tool section');assert.equal(h.posts().length,0);
  });
  await check('Incomplete radio setup opens its tools without starting any task',async()=>{
    const h=harness();h.status.radio_programming.roles.tx.verified=false;h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-tools').open,true);assert.equal(h.el('radio-plan-grid').hidden,false);assert.equal(h.posts().length,0);
    h.status.radio_programming.roles.tx.verified=true;h.ctx.testApi.status(h.status);assert.equal(h.el('radio-tools').open,false);
  });
  for(const state of[
    {pending:true,action:'inspect',role:'rx',success:null},
    {pending:false,action:'verify',role:'tx',success:false,message:'Read-back failed.'},
    {pair:{activation_required:true}},
    {pair:{catalog_changed:true}}
  ])await check(`Radio attention ${JSON.stringify(state)} keeps progress and advanced tools open`,async()=>{
    const h=harness();Object.assign(h.status.radio_programming,state);h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-tools').open,true);assert.equal(h.el('radio-progress-panel').hidden,false);
    h.el('radio-tools').open=false;await h.el('radio-tools').dispatch('toggle');assert.equal(h.el('radio-tools').open,true);
    assert.equal(h.posts().length,0);
  });
  await check('Recovery stays visible even while the radio helper is unavailable',async()=>{
    const h=repairHarness('rx','editor',{radios:{available:false,configured:false}});
    assert.equal(h.el('radio-tools').open,true);assert.equal(h.el('radio-unfinished-notice').hidden,false);assert.equal(h.el('radio-progress-panel').hidden,false);
    assert.equal(h.el('radio-plan-grid').hidden,true);assert.match(h.el('radio-progress-note').textContent,/Review the result/);assert.match(h.el('radio-recovery-link').attrs.href,/radio-editor/);
  });
  await check('Unavailable capabilities hide dead controls while retaining relevant setup notes and restore when configured',async()=>{
    const h=harness({stationIdentification:{available:false},updates:{available:false},radios:{available:false,configured:false}});
    Object.assign(h.status.wifi_visibility,{available:false,profile_hidden:null});Object.assign(h.status.dtmf_control,{available:false,configured:false,bridge_ready:false,code_change:{available:false}});h.ctx.testApi.status(h.status);
    for(const id of['radio-pair-picker','radio-plan-grid','station-id-enabled','wifi-visibility-fields','software-update-actions','dtmf-enabled','dtmf-announcements','dtmf-password-tools'])assert.equal(h.el(id).hidden,true,id);
    assert.match(h.el('station-id-help').textContent,/setup on the Pi/);assert.match(h.el('radio-pair-lock-note').textContent,/Configure|qualification/);assert.equal(h.el('radio-tools').open,false);
    Object.assign(h.status.station_identification,{available:true});Object.assign(h.status.wifi_visibility,{available:true,profile_hidden:false});h.status.software_updates.available=true;Object.assign(h.status.dtmf_control,{available:true,configured:true,bridge_ready:true,code_change:{available:true}});h.ctx.testApi.status(h.status);
    for(const id of['station-id-enabled','wifi-visibility-fields','software-update-actions','dtmf-enabled','dtmf-announcements','dtmf-password-tools'])assert.equal(h.el(id).hidden,false,id);
  });
  await check('DTMF password setup opens only when needed and keeps active or failed changes visible',async()=>{
    const h=harness();h.status.dtmf_control.configured=false;h.ctx.testApi.status(h.status);assert.equal(h.el('dtmf-password-tools').open,true);
    h.status.dtmf_control.configured=true;h.status.dtmf_control.code_change.pending=true;h.ctx.testApi.status(h.status);assert.equal(h.el('dtmf-password-tools').open,true);
    Object.assign(h.status.dtmf_control.code_change,{pending:false,success:false,message:'Password change failed.'});h.ctx.testApi.status(h.status);assert.equal(h.el('dtmf-password-tools').open,true);assert.equal(h.el('dtmf-password-tools').hidden,false);
  });
  await check('Completed bring-up checklist hides only after live control and assigned audio roles are established',async()=>{
    const h=harness();Object.assign(h.status,{is_pi:true,svxlink:{installed:true,mode:'repeater',transmit_control:'enabled'},audio:{capture:[{id:'AIOC_RX'}],playback:[{id:'AIOC_TX'}]}});Object.assign(h.status.radio_programming,{configured_roles:['rx','tx'],preview:false});h.ctx.testApi.status(h.status);
    for(const id of['commissioning-card','checklist-radio','transmit-setup-help'])assert.equal(h.el(id).hidden,true,id);assert.match(h.el('interfaces-description').textContent,/roles are assigned/);
    h.status.svxlink.mode='receive_only';h.ctx.testApi.status(h.status);for(const id of['commissioning-card','checklist-radio','transmit-setup-help'])assert.equal(h.el(id).hidden,false,id);
  });
  await check('Compact DTMF help includes current settings and all three change options',async()=>{
    const {html,inside}=dashboardMarkup();assert.equal(inside('dtmf-help','dtmf-menu-help'),true);assert.equal(inside('dtmf-code-form','dtmf-password-tools'),true);
    assert.match(html,/Choose 0 to hear current settings.*1 for status announcements, 2 for recording, or 3 for automatic station identification/);
  });
  await check('Visible network renders Broadcast On; click reviews stopping broadcast without POST',async()=>{const h=harness();await h.el('wifi-broadcast').dispatch('click');assert.equal(h.el('wifi-confirm-dialog').open,true);assert.equal(h.el('wifi-confirm-title').textContent,'Stop broadcasting the Wi-Fi name?');assert.equal(h.focused,'wifi-confirm-cancel');assert.equal(h.posts().length,0);assert.equal(h.el('wifi-broadcast').attrs['aria-checked'],'true');assert.equal(h.el('wifi-switch-value').textContent,'On');});
  await check('Cancel restores saved Broadcast On, focus, and zero POST',async()=>{const h=harness();await h.el('wifi-broadcast').dispatch('click');await h.el('wifi-confirm-cancel').dispatch('click');assert.equal(h.el('wifi-confirm-dialog').open,false);assert.equal(h.el('wifi-broadcast').attrs['aria-checked'],'true');assert.equal(h.focused,'wifi-broadcast');assert.equal(h.posts().length,0);await h.el('wifi-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);});
  await check('Hidden network renders Broadcast Off; Escape cancels and restores focus',async()=>{const h=harness({hidden:true});await h.el('wifi-broadcast').dispatch('click');assert.equal(h.el('wifi-confirm-title').textContent,'Broadcast the Wi-Fi name?');const e=await h.el('wifi-confirm-dialog').dispatch('cancel');assert.equal(e.defaultPrevented,true);assert.equal(h.el('wifi-confirm-dialog').open,false);assert.equal(h.el('wifi-broadcast').attrs['aria-checked'],'false');assert.equal(h.el('wifi-switch-value').textContent,'Off');assert.equal(h.focused,'wifi-broadcast');assert.equal(h.posts().length,0);});
  await check('Explicit confirmation stops broadcast with one hidden=true POST',async()=>{const h=harness();await h.el('wifi-broadcast').dispatch('click');await h.el('wifi-confirm-apply').dispatch('click');assert.equal(h.posts().length,1);assert.deepEqual(JSON.parse(h.posts()[0].options.body),{hidden:true});assert.equal(h.el('wifi-broadcast').attrs['aria-checked'],'false');assert.equal(h.el('wifi-switch-value').textContent,'Off');assert.equal(h.el('wifi-confirm-dialog').open,false);});
  await check('Explicit confirmation starts broadcast with hidden=false POST',async()=>{const h=harness({hidden:true});await h.el('wifi-broadcast').dispatch('click');await h.el('wifi-confirm-apply').dispatch('click');assert.deepEqual(JSON.parse(h.posts()[0].options.body),{hidden:false});assert.equal(h.el('wifi-broadcast').attrs['aria-checked'],'true');assert.equal(h.el('wifi-switch-value').textContent,'On');});
  for(const [name,change]of[
    ['Profile visibility changed',h=>{h.status.wifi_visibility.profile_hidden=true;}],
    ['Profile SSID changed',h=>{h.status.wifi_visibility.ssid='Other-Box';}],
    ['Request ID changed',h=>{h.status.wifi_visibility.request_id='someone-else';}],
    ['Profile pending',h=>{h.status.wifi_visibility.pending=true;}],
    ['Profile unavailable',h=>{h.status.wifi_visibility.available=false;}],
    ['Signed out session',h=>{h.status.admin.authenticated=false;}],
    ['Changed session token',h=>{h.onStatus=()=>h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'token-2'});}]
  ]) await check(`${name} prevents stale POST`,async()=>{const h=harness();await h.el('wifi-broadcast').dispatch('click');change(h);await h.el('wifi-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('wifi-confirm-dialog').open,false);assert.match(h.el('wifi-visibility-message').textContent,/changed|Applying/);});
  await check('Failed fresh status prevents POST',async()=>{const h=harness();await h.el('wifi-broadcast').dispatch('click');h.failRefresh=true;await h.el('wifi-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('wifi-confirm-dialog').open,false);});
  await check('Cancel during fresh status prevents POST',async()=>{const h=harness();await h.el('wifi-broadcast').dispatch('click');h.onStatus=()=>h.el('wifi-confirm-cancel').dispatch('click');await h.el('wifi-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);});
  await check('Signed-out preview keeps admin options hidden and forced Wi-Fi clicks cannot open a dialog',async()=>{const h=harness({preview:true});assert.equal(h.el('admin-options').hidden,true);assert.equal(h.el('wifi-visibility-fields').disabled,true);await h.el('wifi-broadcast').dispatch('click');assert.equal(h.el('wifi-confirm-dialog').open,false);await h.el('wifi-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);});
  for (const [requested, checked, label] of [[true,'false','Off'],[false,'true','On']]) await check(`Pending requested_hidden=${requested} inverts Broadcast switch`,async()=>{const h=harness({hidden:!requested});Object.assign(h.status.wifi_visibility,{pending:true,requested_hidden:requested});h.ctx.testApi.status(h.status);assert.equal(h.el('wifi-broadcast').attrs['aria-checked'],checked);assert.equal(h.el('wifi-switch-value').textContent,label);assert.equal(h.el('wifi-visibility-fields').disabled,true);assert.equal(h.el('wifi-visibility-state').textContent,'Applying');await h.el('wifi-broadcast').dispatch('click');assert.equal(h.el('wifi-confirm-dialog').open,false);assert.equal(h.posts().length,0);});
  await check('Unavailable unknown visibility is labelled Unknown, disabled and cannot POST',async()=>{const h=harness();Object.assign(h.status.wifi_visibility,{available:false,profile_hidden:null});h.ctx.testApi.status(h.status);assert.equal(h.el('wifi-switch-value').textContent,'Unknown');assert.equal(h.el('wifi-visibility-current').textContent,'Unknown');assert.equal(h.el('wifi-visibility-state').textContent,'Unavailable');assert.equal(h.el('wifi-visibility-fields').disabled,true);await h.el('wifi-broadcast').dispatch('click');assert.equal(h.el('wifi-confirm-dialog').open,false);assert.equal(h.posts().length,0);});
  await check('Configured admin help remains hidden when signed out',async()=>{const h=harness();h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});assert.equal(h.el('admin-setup-help').hidden,true);assert.equal(h.el('admin-login-form').hidden,false);});
  await check('Unknown admin status keeps setup advice hidden',async()=>{const h=harness();h.ctx.testApi.session({configured:false,authenticated:false,csrf_token:null},false);assert.equal(h.el('admin-setup-help').hidden,true);assert.equal(h.el('admin-state').textContent,'Checking');});
  await check('Fresh unconfigured installation shows applicable setup help',async()=>{const h=harness();const status={version:'0.2',is_pi:false,svxlink:{installed:false,mode:'bench'},wifi_visibility:{available:false,profile_hidden:null},network:{hotspot_active:false},live_audio:{configured:false,last_packet_at:null}};h.ctx.testApi.setup(status,{configured:false,authenticated:false,csrf_token:null});for(const id of ['admin-setup-help','network-setup-help','access-setup-help','hotspot-setup-help','checklist-wifi','checklist-pi','audio-setup-help','input-setup-help'])assert.equal(h.el(id).hidden,false,id);});
  await check('Configured inactive AP hides wireless setup help even with helper unavailable',async()=>{const h=harness();Object.assign(h.status.wifi_visibility,{available:false,profile_hidden:false});h.status.network.hotspot_active=false;h.ctx.testApi.status(h.status);for(const id of ['network-setup-help','access-setup-help','hotspot-setup-help','checklist-wifi'])assert.equal(h.el(id).hidden,true,id);});
  await check('Running installed Pi hides SD preparation step',async()=>{const h=harness();Object.assign(h.status,{is_pi:true,svxlink:{installed:true,mode:'bench'}});h.ctx.testApi.status(h.status);assert.equal(h.el('checklist-pi').hidden,true);});
  await check('Assigned AIOC step hides only with configured inspected roles and stable RX capture and TX playback aliases',async()=>{
    const h=harness();Object.assign(h.status,{is_pi:true,audio:{capture:[{id:'AIOC_RX'}],playback:[{id:'AIOC_TX'}]}});
    Object.assign(h.status.radio_programming,{configured_roles:['rx','tx'],preview:false});
    h.ctx.testApi.status(h.status);assert.equal(h.el('checklist-aioc').hidden,true);
    h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});assert.equal(h.el('checklist-aioc').hidden,true);
    const html=fs.readFileSync(path.resolve(__dirname,'../dashboard/static/index.html'),'utf8');
    assert.match(html,/<li id="checklist-radio"><span class="step-number">4<\/span>/);
  });
  await check('Unknown or incomplete AIOC setup keeps its checklist step visible',async()=>{
    for(const change of[
      h=>{h.status.is_pi=undefined;},
      h=>{h.status.radio_programming.configured=false;},
      h=>{h.status.radio_programming.preview=true;},
      h=>{h.status.radio_programming.configured_roles=['rx'];},
      h=>{h.status.radio_programming.roles.rx.inspected=false;},
      h=>{h.status.radio_programming.roles.tx.compatible=false;},
      h=>{h.status.audio.capture=[{id:'AllInOneCable'}];},
      h=>{h.status.audio.playback=[{id:'AIOC_RX'}];},
      h=>{delete h.status.audio;}
    ]){
      const h=harness();Object.assign(h.status,{is_pi:true,audio:{capture:[{id:'AIOC_RX'}],playback:[{id:'AIOC_TX'}]}});
      Object.assign(h.status.radio_programming,{configured_roles:['rx','tx'],preview:false});change(h);
      h.ctx.testApi.status(h.status);assert.equal(h.el('checklist-aioc').hidden,false);
    }
    const h=harness();h.ctx.testApi.status({});assert.equal(h.el('checklist-aioc').hidden,false);
  });
  await check('Feed-enabled flag alone does not hide actual audio setup guidance',async()=>{const h=harness();Object.assign(h.status,{svxlink:{installed:true,mode:'bench'},live_audio:{configured:true,last_packet_at:null}});h.ctx.testApi.status(h.status);assert.equal(h.el('audio-setup-help').hidden,false);assert.equal(h.el('input-setup-help').hidden,false);assert.equal(h.el('input-enabled').attrs['aria-describedby'],'input-enabled-help input-setup-help');});
  await check('Ready monitor without packet leaves First audio setup visible',async()=>{const h=harness();Object.assign(h.status,{svxlink:{installed:true,mode:'receive_only'},live_audio:{configured:true,last_packet_at:null}});h.ctx.testApi.status(h.status);assert.equal(h.el('audio-setup-help').hidden,false);assert.equal(h.el('input-setup-help').hidden,true);assert.equal(h.el('input-enabled').attrs['aria-describedby'],'input-enabled-help');});
  await check('Ready monitor with seen audio hides completed audio setup guidance',async()=>{const h=harness();Object.assign(h.status,{svxlink:{installed:true,mode:'receive_only'},live_audio:{configured:true,last_packet_at:'2026-10-19T19:00:00+00:00'}});h.ctx.testApi.status(h.status);assert.equal(h.el('audio-setup-help').hidden,true);assert.equal(h.el('input-setup-help').hidden,true);});
  await check('Live repeater retains completed receiver setup state',async()=>{const h=harness();Object.assign(h.status,{svxlink:{installed:true,mode:'repeater'},live_audio:{configured:true,last_packet_at:'2026-10-19T19:00:00+00:00'}});h.ctx.testApi.status(h.status);assert.equal(h.el('audio-setup-help').hidden,true);assert.equal(h.el('input-setup-help').hidden,true);});
  await check('Pair polling preserves option nodes and user selection when presets are unchanged',async()=>{const h=pairHarness();const select=h.el('radio-pair-select');select.value='rpt1';const options=select.children;h.ctx.testApi.status(structuredClone(h.status));assert.equal(select.children,options);assert.equal(select.value,'rpt1');h.status.radio_programming.pair_switching.presets=h.status.radio_programming.pair_switching.presets.filter(p=>p.id!=='rpt1');h.ctx.testApi.status(structuredClone(h.status));assert.notEqual(select.children,options);assert.equal(select.value,'default');});
  await check('Settings switch labels follow loaded and unsaved values',async()=>{const h=harness();h.ctx.testApi.settings({input_enabled:true,record_on_startup:false});assert.equal(h.el('input-enabled').checked,true);assert.equal(h.el('input-enabled-value').textContent,'On');assert.equal(h.el('record-startup-value').textContent,'Off');h.el('input-enabled').checked=false;h.el('record-startup').checked=true;await h.el('settings-form').dispatch('input');assert.equal(h.el('input-enabled-value').textContent,'Off');assert.equal(h.el('record-startup-value').textContent,'On');});
  await check('Update card shows installed/latest versions and requires admin for either task',async()=>{const h=harness();assert.equal(h.el('software-current-version').textContent,'0.2.2 · aaaaaaa');assert.equal(h.el('software-latest-version').textContent,'0.2.3 · bbbbbbb');assert.equal(h.el('software-updates-state').textContent,'Update available');h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});assert.equal(h.el('software-check').disabled,true);assert.equal(h.el('software-install').disabled,true);await h.el('software-check').dispatch('click');await h.el('software-install').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('software-confirm-dialog').open,false);});
  await check('Authenticated check sends empty object with CSRF and locks management while pending',async()=>{const h=harness();assert.equal(h.el('settings-fields').disabled,false);await h.el('software-check').dispatch('click');assert.equal(h.posts().length,1);assert.equal(h.posts()[0].path,'/api/software-updates/check');assert.deepEqual(JSON.parse(h.posts()[0].options.body),{});assert.equal(h.posts()[0].options.headers['X-CSRF-Token'],'token-1');assert.equal(h.el('software-updates-state').textContent,'Checking');for(const id of ['software-check','software-install','settings-fields','recording-toggle','wifi-visibility-fields'])assert.equal(h.el(id).disabled,true,id);await h.el('software-check').dispatch('click');assert.equal(h.posts().length,1);});
  await check('Cached update result is displayed but cannot open Install before a page-local check',async()=>{const h=harness();assert.equal(h.el('software-updates-state').textContent,'Update available');assert.equal(h.el('software-install').disabled,true);assert.match(h.el('software-updates-lock-note').textContent,/Check for updates.*session/);await h.el('software-install').dispatch('click');await h.el('software-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('software-confirm-dialog').open,false);});
  await check('Matching successful check initiated by this page enables Install',async()=>{const h=harness();await freshUpdateCheck(h);assert.equal(h.el('software-install').disabled,false);});
  await check('A new page cannot reuse the previous page successful check',async()=>{const h=harness();await freshUpdateCheck(h);const reopened=harness({updates:structuredClone(h.status.software_updates)});assert.equal(reopened.el('software-install').disabled,true);await reopened.el('software-install').dispatch('click');assert.equal(reopened.el('software-confirm-dialog').open,false);});
  for(const [name,change]of[
    ['Installed version',h=>{h.status.software_updates.current_version='0.2.4';}],
    ['Installed commit',h=>{h.status.software_updates.current_commit='c'.repeat(40);}],
    ['Latest version',h=>{h.status.software_updates.latest_version='0.2.4';}],
    ['Latest commit',h=>{h.status.software_updates.latest_commit='c'.repeat(40);}],
    ['Check failed',h=>{h.status.software_updates.success=false;}],
    ['A new install',h=>{h.status.software_updates.action='install';}]
  ])await check(`${name} invalidates the page-local update check`,async()=>{const h=harness();await freshUpdateCheck(h);const before=structuredClone(h.status);change(h);h.ctx.testApi.status(structuredClone(h.status));assert.equal(h.el('software-install').disabled,true);h.ctx.testApi.status(before);assert.equal(h.el('software-install').disabled,true);});
  await check('Sign-out and CSRF replacement revoke the page-local check permanently',async()=>{for(const auth of[{configured:true,authenticated:false,csrf_token:null},{configured:true,authenticated:true,csrf_token:'token-2'}]){const h=harness();await freshUpdateCheck(h);h.ctx.testApi.session(auth);h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'token-1'});assert.equal(h.el('software-install').disabled,true);}});
  await check('Starting a new check requires its new successful result',async()=>{const h=harness();await freshUpdateCheck(h);await h.el('software-check').dispatch('click');assert.equal(h.el('software-install').disabled,true);Object.assign(h.status.software_updates,{pending:false,success:false});h.ctx.testApi.status(structuredClone(h.status));assert.equal(h.el('software-install').disabled,true);Object.assign(h.status.software_updates,{success:true});h.ctx.testApi.status(structuredClone(h.status));assert.equal(h.el('software-install').disabled,true);});
  await check('Same-version manual installation with unknown commit can seed provenance after fresh check',async()=>{const h=harness({updates:{current_version:'0.8.1',current_commit:null,latest_version:'0.8.1'}});assert.equal(h.el('software-install').disabled,true);await freshUpdateCheck(h);assert.equal(h.el('software-install').disabled,false);});
  await check('Install opens Cancel-first dialog without POST; Cancel restores focus',async()=>{const h=harness();await freshUpdateCheck(h);await h.el('software-install').dispatch('click');assert.equal(h.el('software-confirm-dialog').open,true);assert.equal(h.focused,'software-confirm-cancel');assert.match(h.el('software-confirm-description').textContent,/0\.2\.3.*bbbbbbb/);assert.equal(h.posts().length,0);await h.el('software-confirm-cancel').dispatch('click');assert.equal(h.el('software-confirm-dialog').open,false);assert.equal(h.focused,'software-install');await h.el('software-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);});
  await check('Install Escape cancels without POST',async()=>{const h=harness();await freshUpdateCheck(h);await h.el('software-install').dispatch('click');const e=await h.el('software-confirm-dialog').dispatch('cancel');assert.equal(e.defaultPrevented,true);assert.equal(h.el('software-confirm-dialog').open,false);assert.equal(h.focused,'software-install');assert.equal(h.posts().length,0);});
  await check('Confirmed install sends only selected exact commit after a fresh status',async()=>{const h=harness();await freshUpdateCheck(h);await h.el('software-install').dispatch('click');await h.el('software-confirm-apply').dispatch('click');assert.equal(h.posts().length,1);assert.equal(h.posts()[0].path,'/api/software-updates/install');assert.deepEqual(JSON.parse(h.posts()[0].options.body),{commit:'b'.repeat(40)});assert.equal(h.calls[0].path,'/api/status');assert.equal(h.el('software-updates-state').textContent,'Installing');assert.equal(h.el('software-confirm-dialog').open,false);assert.equal(h.el('software-install').disabled,true);});
  for(const [name,change]of[
    ['Latest commit changed',h=>{h.status.software_updates.latest_commit='c'.repeat(40);}],
    ['Installed commit changed',h=>{h.status.software_updates.current_commit='c'.repeat(40);}],
    ['Update request changed',h=>{h.status.software_updates.request_id='other-request';}],
    ['Update no longer available',h=>{h.status.software_updates.update_available=false;}],
    ['Updater became unavailable',h=>{h.status.software_updates.available=false;}],
    ['Another update became pending',h=>{h.status.software_updates.pending=true;}],
    ['Admin signed out',h=>{h.status.admin.authenticated=false;}],
    ['Admin token changed',h=>{h.onStatus=()=>h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'token-2'});}],
    ['Wi-Fi change became pending',h=>{h.status.wifi_visibility.pending=true;}]
  ])await check(`${name} prevents stale install`,async()=>{const h=harness();await freshUpdateCheck(h);await h.el('software-install').dispatch('click');change(h);await h.el('software-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('software-confirm-dialog').open,false);assert.match(h.el('software-updates-message').textContent,/changed/);});
  await check('Failed status refresh prevents install',async()=>{const h=harness();await freshUpdateCheck(h);await h.el('software-install').dispatch('click');h.failRefresh=true;await h.el('software-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('software-confirm-dialog').open,false);});
  await check('Cancel during confirmation status refresh prevents install',async()=>{const h=harness();await freshUpdateCheck(h);await h.el('software-install').dispatch('click');h.onStatus=()=>h.el('software-confirm-cancel').dispatch('click');await h.el('software-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);});
  await check('Update preview permits no checks, dialogs or installs even forced',async()=>{const h=harness({preview:true});assert.equal(h.el('software-updates-state').textContent,'Preview');assert.equal(h.el('software-check').disabled,true);assert.equal(h.el('software-install').disabled,true);await h.el('software-check').dispatch('click');await h.el('software-install').dispatch('click');await h.el('software-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('software-confirm-dialog').open,false);});
  await check('Malformed latest commit blocks install while allowing authenticated checks',async()=>{const h=harness({updates:{latest_commit:'main'}});assert.equal(h.el('software-check').disabled,false);assert.equal(h.el('software-install').disabled,true);await h.el('software-install').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('software-confirm-dialog').open,false);});
  await check('Successful installed status survives restart sign-out and asks for reload',async()=>{const h=harness();Object.assign(h.status.software_updates,{pending:false,action:'install',success:true,current_commit:'b'.repeat(40),current_version:'0.2.3',update_available:false,message:'Dashboard software updated.'});h.status.admin.authenticated=false;h.ctx.testApi.status(h.status);h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});assert.match(h.el('software-updates-message').textContent,/updated.*Reload.*sign in again/);assert.match(h.el('software-updates-message').className,/success/);assert.equal(h.el('software-install').disabled,true);assert.equal(h.el('admin-setup-help').hidden,true);});
  await check('Install dialog describes restart, recording preference, re-sign-in and preserved configuration',async()=>{const html=fs.readFileSync(path.resolve(__dirname, '../dashboard/static/index.html'),'utf8');const dialog=html.match(/<dialog[^>]+id="software-confirm-dialog"[\s\S]*?<\/dialog>/)[0];assert.match(dialog,/restart.*interrupt live listening and recording/);assert.match(dialog,/Record automatically after a restart/);assert.match(dialog,/sign in again/);assert.match(dialog,/Ethernet/);assert.match(dialog,/Wi-Fi, passwords, saved settings, archives, and radio configuration are preserved/);assert.match(dialog,/id="software-confirm-cancel" autofocus/);});
  await check('Recording threshold is a labelled native range with supported bounds and sensitivity endpoints',async()=>{const html=fs.readFileSync(path.resolve(__dirname, '../dashboard/static/index.html'),'utf8');const slider=html.match(/<input\b[^>]*\bid="recording-threshold"[^>]*>/)[0];for(const attribute of ['type="range"','min="-70"','max="-10"','step="1"','value="-45"'])assert.ok(slider.includes(attribute),attribute);assert.match(html,/<label[^>]*for="recording-threshold"/);assert.match(html,/More sensitive/);assert.match(html,/Less sensitive/);assert.match(html,/Lower values catch quieter audio.*Higher values require louder audio/);});
  await check('Saved threshold loads without alteration and exposes its numeric dBFS value',async()=>{const h=harness();h.ctx.testApi.settings({recording_threshold_dbfs:-52});assert.equal(Number(h.el('recording-threshold').value),-52);assert.equal(h.el('recording-threshold-value').textContent,'-52 dBFS');assert.match(h.el('recording-threshold').attrs['aria-valuetext'],/-52 dBFS/);assert.ok(h.el('recording-threshold').attrs['aria-valuetext'].includes(h.el('recording-threshold-description').textContent));assert.equal(h.posts().length,0);});
  await check('Missing saved threshold retains the -45 dBFS default',async()=>{const h=harness();h.ctx.testApi.settings({});assert.equal(Number(h.el('recording-threshold').value),-45);assert.equal(h.el('recording-threshold-value').textContent,'-45 dBFS');assert.match(h.el('recording-threshold-description').textContent,/receiver/);});
  await check('Dragging threshold updates value and understandable sensitivity without POST',async()=>{const h=harness();h.ctx.testApi.settings({recording_threshold_dbfs:-45});h.el('recording-threshold').value='-70';await h.el('settings-form').dispatch('input');assert.equal(h.el('recording-threshold-value').textContent,'-70 dBFS');assert.match(h.el('recording-threshold-description').textContent,/quiet speech.*background noise/);const sensitive=h.el('recording-threshold-description').textContent;h.el('recording-threshold').value='-10';await h.el('settings-form').dispatch('input');assert.equal(h.el('recording-threshold-value').textContent,'-10 dBFS');assert.match(h.el('recording-threshold-description').textContent,/louder audio.*quiet speech may be missed/);assert.notEqual(h.el('recording-threshold-description').textContent,sensitive);assert.match(h.el('recording-threshold').attrs['aria-valuetext'],/-10 dBFS.*louder audio/);assert.match(h.el('settings-message').textContent,/Unsaved.*Save settings/);assert.equal(h.posts().length,0);});
  await check('Polling settings cannot replace an unsaved threshold selection',async()=>{const h=harness();h.ctx.testApi.settings({recording_threshold_dbfs:-45});h.el('recording-threshold').value='-63';await h.el('settings-form').dispatch('input');const description=h.el('recording-threshold-description').textContent;h.ctx.testApi.settings({recording_threshold_dbfs:-20});assert.equal(Number(h.el('recording-threshold').value),-63);assert.equal(h.el('recording-threshold-value').textContent,'-63 dBFS');assert.equal(h.el('recording-threshold-description').textContent,description);assert.equal(h.posts().length,0);});
  await check('Save settings sends the selected threshold as a number and keeps it after refresh',async()=>{const h=harness();h.ctx.testApi.settings({station_name:'Repeater Box',retention_days:7,max_archive_mb:4096,input_enabled:true,record_on_startup:false,recording_threshold_dbfs:-45});h.el('recording-threshold').value='-58';await h.el('settings-form').dispatch('input');assert.equal(h.posts().length,0);await h.el('settings-form').dispatch('submit');const posts=h.posts();assert.equal(posts.length,1);assert.equal(posts[0].path,'/api/settings');const payload=JSON.parse(posts[0].options.body);assert.equal(payload.recording_threshold_dbfs,-58);assert.equal(typeof payload.recording_threshold_dbfs,'number');assert.equal(payload.input_enabled,true);assert.equal(payload.record_on_startup,false);assert.equal(Number(h.el('recording-threshold').value),-58);assert.equal(h.el('recording-threshold-value').textContent,'-58 dBFS');assert.match(h.el('settings-message').textContent,/saved/);});
  await check('Indefinite retention is a labelled native toggle and explains oldest-first rotation',async()=>{const html=fs.readFileSync(path.resolve(__dirname, '../dashboard/static/index.html'),'utf8');const toggle=html.match(/<input\b[^>]*\bid="keep-indefinitely"[^>]*>/)[0];assert.match(toggle,/type="checkbox"/);assert.match(toggle,/role="switch"/);assert.match(toggle,/aria-labelledby="keep-indefinitely-label"/);assert.match(html,/Keep clips until storage space is needed\. The oldest clips are removed first\./);});
  await check('Loaded indefinite retention disables the day field while keeping the stored day count',async()=>{const h=harness();h.ctx.testApi.settings({keep_indefinitely:true,retention_days:23});assert.equal(h.el('keep-indefinitely').checked,true);assert.equal(h.el('keep-indefinitely-value').textContent,'On');assert.equal(h.el('retention-days').disabled,true);assert.equal(Number(h.el('retention-days').value),23);assert.equal(h.posts().length,0);});
  await check('Turning indefinite retention off restores the saved day count without POST',async()=>{const h=harness();h.ctx.testApi.settings({keep_indefinitely:true,retention_days:23});h.el('keep-indefinitely').checked=false;await h.el('settings-form').dispatch('input');assert.equal(h.el('retention-days').disabled,false);assert.equal(Number(h.el('retention-days').value),23);assert.equal(h.el('keep-indefinitely-value').textContent,'Off');assert.equal(h.posts().length,0);});
  await check('Polling preserves an unsaved indefinite-retention choice and day count',async()=>{const h=harness();h.ctx.testApi.settings({keep_indefinitely:false,retention_days:7});h.el('keep-indefinitely').checked=true;await h.el('settings-form').dispatch('input');h.ctx.testApi.settings({keep_indefinitely:false,retention_days:14});assert.equal(h.el('keep-indefinitely').checked,true);assert.equal(h.el('retention-days').disabled,true);assert.equal(Number(h.el('retention-days').value),7);assert.equal(h.posts().length,0);});
  await check('Save sends the indefinite flag and preserved days, then disabling it saves false',async()=>{const h=harness();h.ctx.testApi.settings({station_name:'Repeater Box',retention_days:21,max_archive_mb:4096,input_enabled:true,record_on_startup:false,keep_indefinitely:false,recording_threshold_dbfs:-45});h.el('keep-indefinitely').checked=true;await h.el('settings-form').dispatch('input');await h.el('settings-form').dispatch('submit');let payload=JSON.parse(h.posts()[0].options.body);assert.equal(payload.keep_indefinitely,true);assert.equal(payload.retention_days,21);assert.equal(h.el('retention-days').disabled,true);assert.equal(Number(h.el('retention-days').value),21);h.el('keep-indefinitely').checked=false;await h.el('settings-form').dispatch('input');await h.el('settings-form').dispatch('submit');payload=JSON.parse(h.posts()[1].options.body);assert.equal(payload.keep_indefinitely,false);assert.equal(payload.retention_days,21);assert.equal(h.el('retention-days').disabled,false);});
  await check('Indefinite retention shows an accessible infinity box and hides the numeric day control',async()=>{
    const h=harness();h.ctx.testApi.settings({keep_indefinitely:true,retention_days:23});
    assert.equal(h.el('retention-infinity').hidden,false);assert.equal(h.el('retention-infinity').textContent,'∞');assert.equal(h.el('retention-days').hidden,true);assert.equal(h.el('retention-days-unit').hidden,true);
    assert.equal(Number(h.el('retention-days').value),23);assert.equal(h.el('retention-days-help').textContent,'Clips are kept until storage space is needed.');
    const html=fs.readFileSync(path.resolve(__dirname,'../dashboard/static/index.html'),'utf8');assert.match(html,/<output[^>]*id="retention-infinity"[^>]*aria-label="Keep recordings indefinitely"[^>]*>∞<\/output>/);
  });
  await check('Unsaved day selection survives infinity on/off and incoming settings polls',async()=>{
    const h=harness();h.ctx.testApi.settings({keep_indefinitely:false,retention_days:7});h.el('retention-days').value='23';await h.el('settings-form').dispatch('input');
    h.el('keep-indefinitely').checked=true;await h.el('settings-form').dispatch('input');assert.equal(h.el('retention-infinity').hidden,false);assert.equal(Number(h.el('retention-days').value),23);
    h.ctx.testApi.settings({keep_indefinitely:false,retention_days:14});assert.equal(h.el('retention-infinity').hidden,false);assert.equal(Number(h.el('retention-days').value),23);
    h.el('keep-indefinitely').checked=false;await h.el('settings-form').dispatch('input');assert.equal(h.el('retention-infinity').hidden,true);assert.equal(h.el('retention-days').hidden,false);assert.equal(h.el('retention-days-unit').hidden,false);assert.equal(Number(h.el('retention-days').value),23);
  });
  await check('Saving infinity preserves the last edited numeric day value and restores it after saved roundtrip',async()=>{
    const h=harness();h.ctx.testApi.settings({station_name:'Repeater Box',retention_days:7,max_archive_mb:4096,input_enabled:true,record_on_startup:false,keep_indefinitely:false,recording_threshold_dbfs:-45});
    h.el('retention-days').value='23';h.el('keep-indefinitely').checked=true;await h.el('settings-form').dispatch('input');await h.el('settings-form').dispatch('submit');
    assert.equal(JSON.parse(h.posts()[0].options.body).retention_days,23);assert.equal(h.el('retention-infinity').hidden,false);assert.equal(Number(h.el('retention-days').value),23);
    h.el('keep-indefinitely').checked=false;await h.el('settings-form').dispatch('input');await h.el('settings-form').dispatch('submit');
    assert.equal(JSON.parse(h.posts()[1].options.body).retention_days,23);assert.equal(h.el('retention-infinity').hidden,true);assert.equal(h.el('retention-days').hidden,false);assert.equal(Number(h.el('retention-days').value),23);
  });
  await check('Retention toggle retains admin and update fieldset restrictions',async()=>{const h=harness();h.ctx.testApi.settings({keep_indefinitely:false,retention_days:7});h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});assert.equal(h.el('settings-fields').disabled,true);h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'token-1'});assert.equal(h.el('settings-fields').disabled,false);h.status.software_updates.pending=true;h.ctx.testApi.status(h.status);assert.equal(h.el('settings-fields').disabled,true);assert.equal(h.posts().length,0);});
  await check('Radio plan shows RX transmit inhibition and TX High power with the selected profile',async()=>{const h=harness();assert.equal(h.el('program-rx-frequency').textContent,'145.300 MHz');assert.match(h.el('program-rx-detail').textContent,/88\.5 Hz.*Transmit disabled/);assert.equal(h.el('program-tx-frequency').textContent,'433.500 MHz');assert.match(h.el('program-tx-detail').textContent,/88\.5 Hz.*High power/);assert.match(h.el('radio-rx-identity').textContent,/UV-5RX \/ 5RX profile.*HN5RV011/);assert.equal(h.posts().length,0);});
  await check('Uninspected or incompatible radio cannot be programmed even by a forced click',async()=>{for(const radio of [{inspected:false},{inspected:true,compatible:false,firmware:'unknown'}]){const h=harness();Object.assign(h.status.radio_programming.roles.rx,radio);h.ctx.testApi.status(h.status);assert.equal(h.el('radio-program-rx').disabled,true);assert.equal(h.el('radio-inspect-rx').disabled,false);await h.el('radio-program-rx').dispatch('click');assert.equal(h.el('radio-confirm-dialog').open,false);assert.equal(h.posts().length,0);}});
  await check('Inspection confirmation explains read and backup, with no immediate write',async()=>{const h=harness();await h.el('radio-inspect-rx').dispatch('click');assert.equal(h.el('radio-confirm-title').textContent,'Inspect the RX radio?');assert.match(h.el('radio-confirm-description').textContent,/Read and back up.*RX.*configured UV-5RX \/ 5RX profile/);assert.match(h.el('radio-confirm-impact').textContent,/does not write radio settings/);assert.equal(h.focused,'radio-confirm-cancel');assert.equal(h.posts().length,0);await h.el('radio-confirm-cancel').dispatch('click');assert.equal(h.el('radio-confirm-dialog').open,false);assert.equal(h.focused,'radio-inspect-rx');});
  for(const [role,action] of [['rx','inspect'],['rx','program'],['tx','program'],['tx','verify']])await check(`Confirmed ${action} ${role} sends the narrow role/action payload after fresh status`,async()=>{const h=harness();await h.el(`radio-${action}-${role}`).dispatch('click');assert.equal(h.posts().length,0);await h.el('radio-confirm-apply').dispatch('click');assert.equal(h.posts().length,1);assert.equal(h.calls[0].path,'/api/status');assert.equal(h.posts()[0].path,'/api/radio-programming');assert.deepEqual(JSON.parse(h.posts()[0].options.body),{role,action,...(action==='program'?{catalog_digest:'c'.repeat(64)}:{})});assert.equal(h.posts()[0].options.headers['X-CSRF-Token'],'token-1');assert.equal(h.el('radio-confirm-dialog').open,false);for(const id of ['radio-inspect-rx','radio-program-rx','radio-program-tx','radio-verify-tx','software-check','software-install','settings-fields','recording-toggle','wifi-visibility-fields'])assert.equal(h.el(id).disabled,true,id);});
  await check('Program confirmation names exact plan, backup and automatic verification with conditional recovery',async()=>{const h=harness();await h.el('radio-program-tx').dispatch('click');assert.match(h.el('radio-confirm-description').textContent,/configured UV-5RX \/ 5RX profile.*HN5RV011.*433\.500 MHz.*88\.5 Hz.*High power/);assert.match(h.el('radio-confirm-impact').textContent,/fresh backup.*memory 1.*calibration.*reads back.*automatically.*If verification fails.*Verify.*restart the radio only if it does not reconnect/);assert.doesNotMatch(h.el('radio-confirm-impact').textContent,/off and on|power.cycle/i);assert.equal(h.posts().length,0);const e=await h.el('radio-confirm-dialog').dispatch('cancel');assert.equal(e.defaultPrevented,true);assert.equal(h.posts().length,0);});
  for(const [name,change]of[
    ['Radio plan changed',h=>{h.status.radio_programming.plan.tx.frequency_mhz='433.525000';}],
    ['Inspection identity changed',h=>{h.status.radio_programming.roles.tx.identity='other-radio';}],
    ['Inspection firmware changed',h=>{h.status.radio_programming.roles.tx.firmware='new-firmware';}],
    ['Compatibility changed',h=>{h.status.radio_programming.roles.tx.compatible=false;}],
    ['Radio request changed',h=>{h.status.radio_programming.request_id='other-request';}],
    ['Programming became pending',h=>{h.status.radio_programming.pending=true;}],
    ['Programming became unavailable',h=>{h.status.radio_programming.available=false;}],
    ['Software update became pending',h=>{h.status.software_updates.pending=true;}],
    ['Wi-Fi change became pending',h=>{h.status.wifi_visibility.pending=true;}],
    ['Admin signed out',h=>{h.status.admin.authenticated=false;}],
    ['Admin token changed',h=>{h.onStatus=()=>h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'token-2'});}]
  ])await check(`${name} prevents stale radio programming`,async()=>{const h=harness();await h.el('radio-program-tx').dispatch('click');change(h);await h.el('radio-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('radio-confirm-dialog').open,false);assert.match(h.el('radio-programming-message').textContent,/changed/);});
  await check('Failed status refresh or Cancel while refreshing prevents radio programming',async()=>{for(const cancel of [false,true]){const h=harness();await h.el('radio-program-rx').dispatch('click');if(cancel)h.onStatus=()=>h.el('radio-confirm-cancel').dispatch('click');else h.failRefresh=true;await h.el('radio-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('radio-confirm-dialog').open,false);}});
  await check('Preview, unavailable helper, incomplete config, malformed plan and sign-out disable all radio operations',async()=>{for(const options of [{preview:true},{radios:{available:false}},{radios:{configured:false}},{signedOut:true}]){const h=harness(options);if(options.signedOut)h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});for(const role of ['rx','tx'])for(const action of ['inspect','program','verify']){assert.equal(h.el(`radio-${action}-${role}`).disabled,true);await h.el(`radio-${action}-${role}`).dispatch('click');}await h.el('radio-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('radio-confirm-dialog').open,false);}});
  await check('A failed verification keeps backup and Verify available with conditional reconnect recovery',async()=>{const h=harness();Object.assign(h.status.radio_programming,{role:'tx',action:'program',success:false,verification_required:true,backup_id:'tx-safe-backup.img',message:'The readback did not match the saved candidate.'});Object.assign(h.status.radio_programming.roles.tx,{verified:false,verification_required:true});h.ctx.testApi.status(h.status);assert.equal(h.el('radio-programming-state').textContent,'Verify radio');assert.match(h.el('radio-programming-message').textContent,/readback did not match.*Use the matching Verify.*If the radio does not reconnect, restart/);assert.match(h.el('radio-programming-backup').textContent,/tx-safe-backup.img/);assert.equal(h.el('radio-programming-backup').hidden,false);assert.equal(h.el('radio-verify-tx').disabled,false);assert.match(h.el('radio-tx-identity').textContent,/Verification needed/);await h.el('radio-verify-tx').dispatch('click');assert.match(h.el('radio-confirm-description').textContent,/compare it with the saved upload.*connected and powered/);assert.doesNotMatch(h.el('radio-confirm-description').textContent,/off and on|restart|power.cycle/i);assert.equal(h.posts().length,0);});
  await check('A pending radio task prevents Wi-Fi and software confirmations and settings submission',async()=>{const h=harness({radios:{pending:true,role:'rx',action:'inspect',phase:'reading',message:'Reading the selected radio.'}});assert.equal(h.el('radio-programming-state').textContent,'Inspecting RX');assert.equal(h.el('radio-programming-message').textContent,'Reading the selected radio.');await h.el('wifi-broadcast').dispatch('click');await h.el('software-install').dispatch('click');await h.el('software-check').dispatch('click');await h.el('settings-form').dispatch('submit');await h.el('recording-toggle').dispatch('change');assert.equal(h.posts().length,0);assert.equal(h.el('wifi-confirm-dialog').open,false);assert.equal(h.el('software-confirm-dialog').open,false);});
  await check('Durable unfinished flags during a normal write show progress without recovery instructions',async()=>{
    const h=harness();Object.assign(h.status.radio_programming.roles.rx,{verification_required:true,verified:false});
    for(const phase of['writing','verifying']){
      Object.assign(h.status.radio_programming,{pending:true,role:'rx',action:'program',phase,verification_required:true,message:phase==='writing'?'Writing changes.':'Reading back the result.'});h.ctx.testApi.status(h.status);
      assert.equal(h.el('radio-unfinished-notice').hidden,true);assert.equal(h.el('radio-recovery-link').attrs.href,undefined);
      assert.match(h.el('radio-programming-state').textContent,/Programming RX/);
      assert.doesNotMatch(h.el('radio-programming-message').textContent,/Verify button|restart|power.cycle|repair|Recovery/i);
      assert.equal(h.el('radio-verify-rx').disabled,true);assert.equal(h.el('radio-program-rx').disabled,true);
    }
  });
  await check('Pending programming blocks an already open software confirmation',async()=>{const h=harness();await freshUpdateCheck(h);await h.el('software-install').dispatch('click');h.onStatus=()=>{h.status.radio_programming.pending=true;};await h.el('software-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('software-confirm-dialog').open,false);});
  await check('Unfinished TX upload blocks inspection/programming of either role until TX verifies',async()=>{const h=harness();Object.assign(h.status.radio_programming.roles.tx,{verification_required:true,verified:false});h.status.radio_programming.verification_required=false;h.status.radio_programming.role='rx';h.ctx.testApi.status(h.status);for(const role of ['rx','tx'])for(const action of ['inspect','program']){assert.equal(h.el(`radio-${action}-${role}`).disabled,true);await h.el(`radio-${action}-${role}`).dispatch('click');}assert.equal(h.el('radio-verify-rx').disabled,true);assert.equal(h.el('radio-verify-tx').disabled,false);assert.equal(h.el('radio-programming-state').textContent,'Verify radio');assert.match(h.el('radio-programming-message').textContent,/TX.*Verify.*paused/);assert.equal(h.posts().length,0);});
  await check('Other role becoming unverified invalidates an open programming confirmation',async()=>{const h=harness();await h.el('radio-program-rx').dispatch('click');h.onStatus=()=>{h.status.radio_programming.roles.tx.verification_required=true;};await h.el('radio-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('radio-confirm-dialog').open,false);});
  await check('Live radio progress shows phase block count and a text-only activity log',async()=>{const h=harness({radios:{pending:true,role:'rx',action:'inspect',phase:'reading',progress:{current:64,total:128,percent:50,unit:'blocks'},events:[{time:'2026-10-28T19:00:00Z',message:'<b>Reading radio</b>'}]}});assert.equal(h.el('radio-progress-panel').hidden,false);assert.equal(h.el('radio-progress-bar').value,50);assert.match(h.el('radio-progress-count').textContent,/64 \/ 128 blocks.*50%/);assert.equal(h.el('radio-progress-phase').textContent,'RX · Reading radio');assert.match(h.el('radio-progress-events').textContent,/<b>Reading radio<\/b>/);});
  await check('Phase without a measured count uses indeterminate progress',async()=>{const h=harness({radios:{pending:true,role:'tx',phase:'backing-up',progress:null}});assert.equal(h.el('radio-progress-panel').hidden,false);assert.equal(h.el('radio-progress-bar').attrs.value,undefined);assert.equal(h.el('radio-progress-count').textContent,'In progress');assert.match(h.el('radio-progress-phase').textContent,/Saving local backup/);h.status.radio_programming.progress={current:7,total:6,percent:101,unit:'blocks'};h.ctx.testApi.status(h.status);assert.equal(h.el('radio-progress-bar').attrs.value,undefined);});
  await check('Completed and failed radio operations retain their activity without pretending failure completed',async()=>{const h=harness({radios:{pending:false,role:'rx',phase:'verified',success:true,progress:{current:131,total:131,percent:100,unit:'blocks'},events:[{time:'2026-10-28T19:00:01Z',message:'Verification complete'}]}});assert.equal(h.el('radio-progress-bar').value,100);assert.match(h.el('radio-progress-phase').textContent,/Verification complete/);Object.assign(h.status.radio_programming,{phase:'verification-required',success:false,verification_required:true,progress:null});h.ctx.testApi.status(h.status);assert.match(h.el('radio-progress-phase').textContent,/Verification needs attention/);assert.equal(h.el('radio-progress-bar').attrs.value,undefined);assert.match(h.el('radio-progress-events').textContent,/Verification complete/);});
  await check('Custom radio changes are identified separately from the default plan',async()=>{const h=harness();h.status.radio_programming.roles.rx.program_kind='editor';h.ctx.testApi.status(h.status);assert.match(h.el('radio-rx-identity').textContent,/Custom edits verified; default plan shown/);assert.match(h.el('program-rx-detail').textContent,/High power.*Transmit disabled.*Auto Lock on/);});
  await check('Pending radio polls lightweight progress and stops polling when complete',async()=>{const h=harness({radios:{pending:true}});const calls=[];h.ctx.fetch=async(path)=>{calls.push(path);return{ok:true,json:async()=>({radio_programming:{...h.status.radio_programming,pending:false,success:true,phase:'verified'}})}};await h.ctx.testApi.progress();await h.ctx.testApi.progress();assert.deepEqual(calls,['/api/radio-programming/status']);assert.match(h.el('radio-progress-phase').textContent,/Verification complete/);});
  await check('A lost live progress connection retains the operation and warns against repeated upload',async()=>{const h=harness({radios:{pending:true}});h.ctx.fetch=async()=>{throw new Error('offline')};await h.ctx.testApi.progress();assert.match(h.el('radio-progress-note').textContent,/reconnecting automatically.*Do not repeat the upload/);assert.equal(h.status.radio_programming.pending,true);});
  await check('Concurrent progress refreshes share a single in-flight request',async()=>{const h=harness({radios:{pending:true}});let release,count=0;h.ctx.fetch=()=>{count++;return new Promise(r=>{release=()=>r({ok:true,json:async()=>({radio_programming:h.status.radio_programming})})})};const first=h.ctx.testApi.progress();await h.ctx.testApi.progress();assert.equal(count,1);release();await first;});
  await check('External programming failure names the unfinished connection and links to its recovery editor',async()=>{
    const h=harness();
    Object.assign(h.status.radio_programming,{role:'programmer',action:'verify',success:false,phase:'verification-required',verification_required:true,events:[{time:'2026-10-28T20:00:00Z',message:'Verification requires review'}]});
    h.status.radio_programming.roles.programmer={verification_required:true};h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-programming-state').textContent,'Verify external programmer');
    assert.equal(h.el('radio-unfinished-notice').hidden,false);
    assert.equal(h.el('radio-unfinished-title').textContent,'External programmer upload unfinished');
    assert.equal(h.el('radio-recovery-link').attrs.href,'/radio-editor');
    assert.match(h.el('radio-unfinished-detail').textContent,/select “External programmer”/);
    assert.match(h.el('radio-unfinished-detail').textContent,/same physical radio.*Verify upload/);
    assert.match(h.el('radio-programming-message').textContent,/external programmer.*unfinished upload/);
    assert.match(h.el('radio-programming-lock-note').textContent,/External programmer.*radio-editor/);
    assert.match(h.el('radio-progress-phase').textContent,/Programming port/);
    for(const role of['rx','tx'])for(const action of['inspect','program','verify'])assert.equal(h.el(`radio-${action}-${role}`).disabled,true);
    await h.el('radio-recovery-link').dispatch('click');assert.equal(h.posts().length,0);
  });
  await check('External recovery guidance uses the unfinished original UV5R profile without changing box plans',async()=>{
    const h=harness();h.status.radio_programming.roles.programmer={verification_required:true,model:'UV5R'};h.ctx.testApi.status(h.status);
    for(const id of['radio-unfinished-detail','radio-programming-lock-note','radio-programming-message'])assert.match(h.el(id).textContent,/UV-5R \/ UV5R/);
    assert.doesNotMatch(h.el('radio-unfinished-detail').textContent,/UV-5RX/);
    assert.match(h.el('radio-rx-identity').textContent,/UV-5RX \/ 5RX profile/);assert.equal(h.el('program-rx-frequency').textContent,'145.300 MHz');
    assert.equal(h.posts().length,0);
  });
  await check('An older external upload stays prominent after RX and TX have verified successfully',async()=>{
    const h=pairHarness();
    Object.assign(h.status.radio_programming,{role:'rx',action:'verify',success:true,phase:'verified',verification_required:false,message:'RX settings verified successfully.'});
    h.status.radio_programming.roles.rx.program_kind='editor';
    h.status.radio_programming.roles.programmer={verification_required:true};h.ctx.testApi.status(h.status);
    assert.match(h.el('radio-rx-identity').textContent,/Custom edits verified/);
    assert.match(h.el('radio-tx-identity').textContent,/Saved upload verified/);
    assert.equal(h.el('radio-programming-state').textContent,'Verify external programmer');
    assert.equal(h.el('radio-unfinished-notice').hidden,false);
    assert.match(h.el('radio-programming-message').textContent,/external programmer.*RX and TX show verified/);
    assert.doesNotMatch(h.el('radio-programming-message').textContent,/RX settings verified successfully/);
    assert.match(h.el('radio-pair-lock-note').textContent,/external programmer.*unfinished/);
    assert.equal(h.el('radio-pair-change').disabled,true);
    for(const role of['rx','tx'])for(const action of['inspect','program','verify']){
      assert.equal(h.el(`radio-${action}-${role}`).disabled,true);
      await h.el(`radio-${action}-${role}`).dispatch('click');
    }
    assert.equal(h.el('radio-confirm-dialog').open,false);assert.equal(h.posts().length,0);
  });
  await check('Unfinished RX and TX are named and keep their existing Verify actions available',async()=>{
    const h=harness();
    for(const role of['rx','tx'])Object.assign(h.status.radio_programming.roles[role],{verification_required:true,verified:false});
    h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-unfinished-title').textContent,'Uploads unfinished: RX radio, TX radio');
    assert.match(h.el('radio-programming-lock-note').textContent,/RX radio, TX radio need verification/);
    assert.match(h.el('radio-unfinished-detail').textContent,/matching Verify button/);
    for(const role of['rx','tx']){
      assert.equal(h.el(`radio-verify-${role}`).disabled,false);
      for(const action of['inspect','program'])assert.equal(h.el(`radio-${action}-${role}`).disabled,true);
    }
    assert.equal(h.posts().length,0);
  });
  await check('Recovery notice waits during a running task and disappears only after every unfinished record clears',async()=>{
    const h=harness();assert.equal(h.el('radio-unfinished-notice').hidden,true);assert.equal(h.el('radio-recovery-link').attrs.href,undefined);
    h.status.radio_programming.roles.programmer={verification_required:true};
    h.status.radio_programming.pending=true;h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-unfinished-notice').hidden,true);
    assert.equal(h.el('radio-unfinished-detail').textContent,'');
    assert.equal(h.el('radio-recovery-link').attrs.href,undefined);
    h.status.radio_programming.pending=false;h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-unfinished-notice').hidden,false);
    h.status.radio_programming.roles.programmer.verification_required=false;h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-unfinished-notice').hidden,true);assert.equal(h.el('radio-unfinished-title').textContent,'');
    assert.equal(h.el('radio-unfinished-detail').textContent,'');assert.equal(h.el('radio-recovery-link').attrs.href,undefined);
    assert.equal(h.el('radio-inspect-rx').disabled,false);assert.equal(h.el('radio-program-rx').disabled,false);assert.equal(h.posts().length,0);
  });
  await check('Saved-upload repair is hidden without a supported unfinished candidate and cannot be forced',async()=>{
    for(const details of[{}, {verification_required:true,program_kind:'editor'}, {verification_required:true,program_kind:'editor',pending_id:'invalid'}, {verification_required:true,program_kind:'pair',pending_id:'e'.repeat(32)}]){
      const h=harness();Object.assign(h.status.radio_programming.roles.rx,details);h.ctx.testApi.status(h.status);
      assert.equal(h.el('radio-repair-rx').hidden,true);assert.equal(h.el('radio-repair-rx').disabled,true);
      await h.el('radio-repair-rx').dispatch('click');await h.el('radio-confirm-apply').dispatch('click');
      assert.equal(h.el('radio-confirm-dialog').open,false);assert.equal(h.posts().length,0);
    }
  });
  for(const role of['rx','tx'])for(const kind of['editor','repeater'])await check(`Saved ${kind} ${role} repair reviews the candidate and only sends its pinned ID after confirmation`,async()=>{
    const h=repairHarness(role,kind);const other=role==='rx'?'tx':'rx';
    assert.equal(h.el(`radio-repair-${role}`).hidden,false);assert.equal(h.el(`radio-repair-${role}`).disabled,false);
    assert.equal(h.el(`radio-repair-${other}`).hidden,true);
    await h.el(`radio-repair-${role}`).dispatch('click');
    assert.equal(h.el('radio-confirm-dialog').open,true);assert.equal(h.focused,'radio-confirm-cancel');assert.equal(h.posts().length,0);
    assert.match(h.el('radio-confirm-description').textContent,/private saved candidate.*neighboring bytes.*does not apply a new default plan/);
    assert.match(h.el('radio-confirm-impact').textContent,/same physical radio.*fresh backup.*only affected pages.*paused.*every unfinished upload verify/);
    await h.el('radio-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,1);assert.equal(h.calls[0].path,'/api/status');
    assert.equal(h.posts()[0].path,'/api/radio-programming');assert.equal(h.posts()[0].options.headers['X-CSRF-Token'],'token-1');
    assert.deepEqual(JSON.parse(h.posts()[0].options.body),{role,action:'repair-upload',pending_id:'e'.repeat(32)});
    assert.equal(h.el(`radio-repair-${role}`).disabled,true);assert.match(h.el('radio-programming-state').textContent,/Repairing/);
  });
  await check('Canceling a saved-upload repair restores focus and never adopts or programs anything',async()=>{
    const h=repairHarness();await h.el('radio-repair-rx').dispatch('click');await h.el('radio-confirm-cancel').dispatch('click');
    assert.equal(h.el('radio-confirm-dialog').open,false);assert.equal(h.focused,'radio-repair-rx');
    await h.el('radio-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
  });
  for(const [name,change]of[
    ['Saved candidate changed',h=>{h.status.radio_programming.roles.rx.pending_id='a'.repeat(32);}],
    ['Upload kind changed',h=>{h.status.radio_programming.roles.rx.program_kind='repeater';}],
    ['Radio identity changed',h=>{h.status.radio_programming.roles.rx.identity='other';}],
    ['Radio firmware changed',h=>{h.status.radio_programming.roles.rx.firmware='other';}],
    ['Radio request changed',h=>{h.status.radio_programming.request_id='other';}],
    ['Verification resolved',h=>{h.status.radio_programming.roles.rx.verification_required=false;}],
    ['Paired recovery appeared',h=>{h.status.radio_programming.pair={verification_required:true};}],
    ['Other paired upload appeared',h=>{Object.assign(h.status.radio_programming.roles.tx,{verification_required:true,program_kind:'pair'});}],
    ['Radio work started',h=>{h.status.radio_programming.pending=true;}],
    ['Software update started',h=>{h.status.software_updates.pending=true;}],
    ['Wi-Fi change started',h=>{h.status.wifi_visibility.pending=true;}],
    ['Admin signed out',h=>{h.status.admin.authenticated=false;}],
    ['Admin token changed',h=>{h.onStatus=()=>h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'other'});}]
  ])await check(`${name} prevents a stale saved-upload repair`,async()=>{
    const h=repairHarness();await h.el('radio-repair-rx').dispatch('click');change(h);await h.el('radio-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,0);assert.equal(h.el('radio-confirm-dialog').open,false);assert.match(h.el('radio-programming-message').textContent,/changed/);
  });
  await check('Preview, sign-out and maintenance interlocks reject forced saved-upload repair clicks',async()=>{
    for(const options of[{preview:true},{radios:{available:false}},{radios:{configured:false}},{radios:{pending:true}},{updates:{pending:true}},{signedOut:true}]){
      const h=repairHarness('rx','editor',options);if(options.signedOut)h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});
      assert.equal(h.el('radio-repair-rx').disabled,true);await h.el('radio-repair-rx').dispatch('click');await h.el('radio-confirm-apply').dispatch('click');
      assert.equal(h.el('radio-confirm-dialog').open,false);assert.equal(h.posts().length,0);
    }
  });
  await check('External-only recovery cannot be retargeted through the RX or TX repair buttons',async()=>{
    const h=harness();h.status.radio_programming.roles.programmer={verification_required:true,program_kind:'editor',pending_id:'e'.repeat(32)};h.ctx.testApi.status(h.status);
    for(const role of['rx','tx']){assert.equal(h.el(`radio-repair-${role}`).hidden,true);await h.el(`radio-repair-${role}`).dispatch('click');}
    await h.el('radio-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
  });
  await check('Admin options and recording management default hidden in HTML while public playback remains outside',async()=>{
    const html=fs.readFileSync(path.resolve(__dirname,'../dashboard/static/index.html'),'utf8');
    assert.match(html,/<div id="admin-options" hidden>/);assert.match(html,/<div class="recorder-control" id="recorder-admin-controls" hidden>/);
    const position=html.indexOf('id="admin-options"');assert.ok(html.indexOf('id="admin-title"')<position);
    for(const heading of['access-title','wifi-visibility-title','radio-pair-title','radio-programming-title','software-updates-title','station-id-title','dtmf-title','settings-title'])assert.ok(html.indexOf(`id="${heading}"`)>position,heading);
    assert.ok(html.indexOf('id="recording-workspace"')<position);assert.ok(html.indexOf('id="listen-button"')<position);
  });
  await check('Unknown and signed-out sessions hide all admin options; authentication reveals them',async()=>{
    const h=harness();assert.equal(h.el('admin-options').hidden,false);assert.equal(h.el('recorder-admin-controls').hidden,false);
    h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null},false);assert.equal(h.el('admin-options').hidden,true);assert.equal(h.el('recorder-admin-controls').hidden,true);
    h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});assert.equal(h.el('admin-options').hidden,true);assert.equal(h.el('admin-login-form').hidden,false);
    h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'new-token'});assert.equal(h.el('admin-options').hidden,false);assert.equal(h.el('recorder-admin-controls').hidden,false);
  });
  await check('Expired authentication from status polling hides options and closes every pending admin dialog',async()=>{
    const h=harness();for(const id of['wifi-confirm-dialog','software-confirm-dialog','radio-confirm-dialog','radio-pair-confirm-dialog','dtmf-confirm-dialog','station-id-confirm-dialog'])h.el(id).showModal();
    h.el('dtmf-new-code').value='ABCD12';h.el('dtmf-admin-password').value='current-admin';h.el('radio-pair-confirm-password').value='current-admin';
    h.status.admin.authenticated=false;await h.ctx.testApi.refresh();
    assert.equal(h.el('admin-options').hidden,true);assert.equal(h.el('recorder-admin-controls').hidden,true);
    for(const id of['wifi-confirm-dialog','software-confirm-dialog','radio-confirm-dialog','radio-pair-confirm-dialog','dtmf-confirm-dialog','station-id-confirm-dialog'])assert.equal(h.el(id).open,false);
    for(const id of['dtmf-new-code','dtmf-admin-password','radio-pair-confirm-password'])assert.equal(h.el(id).value,'');
  });
  await check('Sign-out hides options immediately after confirmation and clears the visible code',async()=>{
    const h=harness();h.el('dtmf-new-code').value='ABCD12';h.el('dtmf-admin-password').value='current-admin';
    await h.el('logout-button').dispatch('click');assert.equal(h.posts().length,1);assert.equal(h.posts()[0].path,'/api/admin/logout');
    assert.equal(h.el('admin-options').hidden,true);assert.equal(h.el('recorder-admin-controls').hidden,true);assert.equal(h.el('dtmf-new-code').value,'');assert.equal(h.el('dtmf-admin-password').value,'');
  });
  await check('Automatic identification has an accessible switch and separate Cancel-default dialog inside admin options',async()=>{
    const html=fs.readFileSync(path.resolve(__dirname,'../dashboard/static/index.html'),'utf8');
    const control=html.match(/<button[^>]*id="station-id-enabled"[^>]*>/)?.[0];assert.ok(control);
    for(const attr of['role="switch"','aria-checked="true"','aria-labelledby="station-id-switch-label"','aria-describedby="station-id-help station-id-effect"','aria-haspopup="dialog"','aria-controls="station-id-confirm-dialog"','disabled'])assert.ok(control.includes(attr),attr);
    assert.match(html,/<dialog[^>]*id="station-id-confirm-dialog"[^>]*aria-labelledby="station-id-confirm-title"[^>]*aria-describedby="station-id-confirm-description"/);
    assert.match(html,/<button[^>]*id="station-id-confirm-cancel"[^>]*autofocus/);
    assert.ok(html.indexOf('id="station-id-title"')>html.indexOf('id="admin-options"'));
    assert.ok(html.indexOf('id="station-id-title"')<html.indexOf('id="settings-title"'));
  });
  await check('Automatic identification defaults On and opens a review without changing the saved switch',async()=>{
    const h=harness();assert.equal(h.el('station-id-enabled').disabled,false);assert.equal(h.el('station-id-enabled').attrs['aria-checked'],'true');
    await h.el('station-id-enabled').dispatch('click');assert.equal(h.el('station-id-confirm-dialog').open,true);assert.equal(h.focused,'station-id-confirm-cancel');
    assert.equal(h.el('station-id-confirm-title').textContent,'Disable automatic station identification?');assert.equal(h.el('station-id-enabled').attrs['aria-checked'],'true');assert.equal(h.posts().length,0);
    assert.match(h.el('station-id-confirm-description').textContent,/future automatic.*already playing may finish.*manually.*DTMF prompts are unaffected/);
  });
  for(const action of['cancel','escape','close'])await check(`Automatic identification ${action} discards the proposal and prevents a forced save`,async()=>{
    const h=harness();await h.el('station-id-enabled').dispatch('click');
    if(action==='cancel')await h.el('station-id-confirm-cancel').dispatch('click');
    else if(action==='escape')assert.equal((await h.el('station-id-confirm-dialog').dispatch('cancel')).defaultPrevented,true);
    else h.el('station-id-confirm-dialog').close();
    assert.equal(h.el('station-id-confirm-dialog').open,false);await h.el('station-id-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
    assert.equal(h.el('station-id-enabled').attrs['aria-checked'],'true');
    if(action!=='close')assert.equal(h.focused,'station-id-enabled');
  });
  for(const enabled of[true,false])await check(`Confirmed automatic identification ${enabled?'off':'on'} uses fresh status and sends only its flag with admin CSRF`,async()=>{
    const h=harness({stationIdentification:{enabled}});h.status.settings.record_on_startup=true;
    await h.el('station-id-enabled').dispatch('click');assert.equal(h.posts().length,0);await h.el('station-id-confirm-apply').dispatch('click');
    assert.equal(h.calls[0].path,'/api/status');assert.equal(h.posts().length,1);const post=h.posts()[0];assert.equal(post.path,'/api/station-identification');
    assert.deepEqual(JSON.parse(post.options.body),{enabled:!enabled});assert.equal(post.options.headers['X-CSRF-Token'],'token-1');
    assert.equal(h.el('station-id-enabled').attrs['aria-checked'],String(!enabled));assert.equal(h.el('station-id-switch-value').textContent,enabled?'Off':'On');assert.equal(h.el('station-id-confirm-dialog').open,false);
    assert.equal(h.status.settings.record_on_startup,true);assert.equal(h.status.dtmf_control.enabled,false);assert.equal(h.status.dtmf_control.status_announcements,true);
    if(enabled)assert.match(h.el('station-id-help').textContent,/manually.*DTMF prompts are unaffected/);
  });
  for(const [label,change] of[
    ['saved value',h=>{h.status.station_identification.enabled=false;}],
    ['availability',h=>{h.status.station_identification.available=false;}],
    ['missing status',h=>{delete h.status.station_identification;}],
    ['malformed value',h=>{h.status.station_identification.enabled='true';}],
    ['radio task',h=>{h.status.radio_programming.pending=true;}],
    ['unfinished upload',h=>{h.status.radio_programming.roles.rx.verification_required=true;}],
    ['unfinished pair',h=>{h.status.radio_programming.pair={verification_required:true};}],
    ['software task',h=>{h.status.software_updates.pending=true;}],
    ['Wi-Fi task',h=>{h.status.wifi_visibility.pending=true;}],
    ['DTMF password task',h=>{h.status.dtmf_control.code_change.pending=true;}],
    ['admin configuration',h=>{h.status.admin.configured=false;}],
    ['expired admin session',h=>{h.status.admin.authenticated=false;}],
    ['new admin session',h=>{h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'token-2'});}]
  ])await check(`Fresh ${label} change blocks a stale automatic identification save`,async()=>{
    const h=harness();await h.el('station-id-enabled').dispatch('click');h.onStatus=()=>change(h);await h.el('station-id-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,0);assert.equal(h.el('station-id-confirm-dialog').open,false);assert.match(h.el('station-id-message').textContent,/changed/);
  });
  for(const [label,change] of[
    ['signed out',h=>{h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});}],
    ['unknown session',h=>{h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'token-1'},false);}],
    ['missing CSRF',h=>{h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:null});}],
    ['unavailable',h=>{h.status.station_identification.available=false;}],
    ['unknown value',h=>{h.status.station_identification.enabled=null;}],
    ['radio programming',h=>{h.status.radio_programming.pending=true;}],
    ['software update',h=>{h.status.software_updates.pending=true;}],
    ['Wi-Fi change',h=>{h.status.wifi_visibility.pending=true;}],
    ['DTMF password change',h=>{h.status.dtmf_control.code_change.pending=true;}]
  ])await check(`${label} automatic identification cannot be forced open or saved`,async()=>{
    const h=harness();change(h);h.ctx.testApi.status(h.status);assert.equal(h.el('station-id-enabled').disabled,true);
    await h.el('station-id-enabled').dispatch('click');await h.el('station-id-confirm-apply').dispatch('click');assert.equal(h.el('station-id-confirm-dialog').open,false);assert.equal(h.posts().length,0);
  });
  await check('Failed fresh status and cancellation during fresh status prevent automatic identification POST',async()=>{
    for(const mode of['offline','cancel']){
      const h=harness();await h.el('station-id-enabled').dispatch('click');
      if(mode==='offline')h.failRefresh=true;else h.onStatus=()=>h.el('station-id-confirm-cancel').dispatch('click');
      await h.el('station-id-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);assert.equal(h.el('station-id-confirm-dialog').open,false);
    }
  });
  await check('Automatic identification save errors show the server message without changing the saved state',async()=>{
    const h=harness();h.stationIdError='Radio maintenance started. Try again after verification.';
    await h.el('station-id-enabled').dispatch('click');await h.el('station-id-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,1);assert.equal(h.el('station-id-enabled').attrs['aria-checked'],'true');assert.match(h.el('station-id-message').textContent,/Radio maintenance started/);assert.equal(h.el('station-id-enabled').disabled,false);
  });
  await check('Unreadable identification save response prompts a refresh and never resends automatically',async()=>{
    const h=harness();h.stationIdResult={station_identification:{enabled:'false'}};
    await h.el('station-id-enabled').dispatch('click');await h.el('station-id-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,1);assert.match(h.el('station-id-message').textContent,/unreadable.*Refresh/);assert.equal(h.el('station-id-enabled').attrs['aria-checked'],'false');
  });
  await check('Unavailable browser dialog support prevents an automatic identification change',async()=>{
    const h=harness();h.el('station-id-confirm-dialog').showModal=undefined;await h.el('station-id-enabled').dispatch('click');await h.el('station-id-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,0);assert.match(h.el('station-id-message').textContent,/browser cannot show/);
  });
  await check('DTMF control is a confirmed admin switch and Cancel sends no change',async()=>{
    const h=harness();assert.equal(h.el('dtmf-enabled').attrs['aria-checked'],'false');
    await h.el('dtmf-enabled').dispatch('click');assert.equal(h.el('dtmf-confirm-dialog').open,true);assert.equal(h.focused,'dtmf-confirm-cancel');assert.equal(h.posts().length,0);
    await h.el('dtmf-confirm-cancel').dispatch('click');await h.el('dtmf-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
  });
  await check('Confirmed DTMF enable sends only the flag with admin CSRF after fresh status',async()=>{
    const h=harness();await h.el('dtmf-enabled').dispatch('click');await h.el('dtmf-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,1);const post=h.posts()[0];assert.equal(post.path,'/api/dtmf-control');assert.deepEqual(JSON.parse(post.options.body),{enabled:true});
    assert.equal(post.options.headers['X-CSRF-Token'],'token-1');assert.equal(h.calls[0].path,'/api/status');assert.equal(h.el('dtmf-enabled').attrs['aria-checked'],'true');
  });
  await check('DTMF disable also requires confirmation and does not alter recording startup settings',async()=>{
    const h=harness();h.status.dtmf_control.enabled=true;h.status.settings.record_on_startup=true;h.ctx.testApi.status(h.status);
    await h.el('dtmf-enabled').dispatch('click');assert.match(h.el('dtmf-confirm-description').textContent,/Current recording.*startup preference.*stay/);
    await h.el('dtmf-confirm-apply').dispatch('click');assert.deepEqual(JSON.parse(h.posts()[0].options.body),{enabled:false});assert.equal(h.status.settings.record_on_startup,true);
  });
  for(const [label,change] of[
    ['configuration',h=>{h.status.dtmf_control.configured=false;}],
    ['bridge',h=>{h.status.dtmf_control.bridge_ready=false;h.status.dtmf_control.available=false;}],
    ['enabled state',h=>{h.status.dtmf_control.enabled=true;}],
    ['unfinished pair',h=>{h.status.radio_programming.pair={verification_required:true};}],
    ['radio task',h=>{h.status.radio_programming.pending=true;}],
    ['admin session',h=>{h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});}]
  ])await check(`Changed ${label} prevents a stale DTMF toggle`,async()=>{
    const h=harness();await h.el('dtmf-enabled').dispatch('click');h.onStatus=()=>change(h);await h.el('dtmf-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
  });
  await check('Unavailable, unconfigured and signed-out DTMF controls cannot be forced',async()=>{
    for(const mode of['unavailable','unconfigured','signed-out']){
      const h=harness();if(mode==='signed-out')h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});else{Object.assign(h.status.dtmf_control,{available:false,configured:mode!=='unconfigured',bridge_ready:false});h.ctx.testApi.status(h.status);}
      assert.equal(h.el('dtmf-enabled').disabled,true);await h.el('dtmf-enabled').dispatch('click');await h.el('dtmf-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
    }
  });
  await check('Spoken status preference is confirmed separately and sends only its boolean flag',async()=>{
    const h=harness();assert.equal(h.el('dtmf-announcements').attrs['aria-checked'],'true');
    await h.el('dtmf-announcements').dispatch('click');assert.equal(h.posts().length,0);assert.match(h.el('dtmf-confirm-description').textContent,/Menu prompts and basic confirmation/);
    await h.el('dtmf-confirm-apply').dispatch('click');assert.deepEqual(JSON.parse(h.posts()[0].options.body),{status_announcements:false});
    assert.equal(h.status.dtmf_control.enabled,false);assert.equal(h.el('dtmf-announcements').attrs['aria-checked'],'false');
  });
  await check('New DTMF password uses one visible bounded field without a repeat field',async()=>{
    const html=fs.readFileSync(path.resolve(__dirname,'../dashboard/static/index.html'),'utf8');
    const input=html.match(/<input[^>]*id="dtmf-new-code"[^>]*>/)?.[0];assert.ok(input);
    for(const attr of['type="text"','maxlength="6"','minlength="6"','pattern="[0-9ABCD]{6}"','autocapitalize="characters"','autocorrect="off"','spellcheck="false"'])assert.ok(input.includes(attr));
    assert.equal(html.includes('dtmf-confirm-code'),false);
  });
  await check('DTMF typing uppercases a-d, strips unsupported characters and truncates at six',async()=>{
    const h=harness();for(const [raw,wanted] of[['a','A'],['ab12cd','AB12CD'],['aB!2#*c3d4','AB2C3D'],['123456789','123456'],['zZxy!@','']]){
      h.el('dtmf-new-code').value=raw;h.el('dtmf-new-code').setSelectionRange(raw.length,raw.length);await h.el('dtmf-new-code').dispatch('input');
      assert.equal(h.el('dtmf-new-code').value,wanted);assert.equal(h.el('dtmf-new-code').selectionStart,wanted.length);
    }
  });
  await check('DTMF filtering preserves a middle edit cursor and paste keeps valid characters after invalid ones',async()=>{
    const h=harness();h.el('dtmf-new-code').value='a!b12';h.el('dtmf-new-code').setSelectionRange(3,3);await h.el('dtmf-new-code').dispatch('input');
    assert.equal(h.el('dtmf-new-code').value,'AB12');assert.equal(h.el('dtmf-new-code').selectionStart,2);
    h.el('dtmf-new-code').value='';h.el('dtmf-new-code').setSelectionRange(0,0);
    const pasted=await h.el('dtmf-new-code').dispatch('paste',{clipboardData:{getData:()=> '!@#$abcd123456'}});
    assert.equal(pasted.defaultPrevented,true);assert.equal(h.el('dtmf-new-code').value,'ABCD12');assert.equal(h.el('dtmf-new-code').selectionStart,6);
    h.el('dtmf-new-code').value='123456';h.el('dtmf-new-code').setSelectionRange(2,4);
    await h.el('dtmf-new-code').dispatch('paste',{clipboardData:{getData:()=> 'a?b'}});
    assert.equal(h.el('dtmf-new-code').value,'12AB56');assert.equal(h.el('dtmf-new-code').selectionStart,4);
  });
  await check('New DTMF code is exactly six supported characters and needs a fresh admin password',async()=>{
    for(const [code,password] of[['12345','admin'],['1234567','admin'],['ABCd12','admin'],['ABC#12','admin'],['ABCD*1','admin'],['ABCD12','']]){
      const h=harness();h.el('dtmf-new-code').value=code;h.el('dtmf-admin-password').value=password;
      await h.el('dtmf-code-form').dispatch('submit');assert.equal(h.posts().length,0);assert.match(h.el('dtmf-code-message').textContent,/six-character/);
      assert.equal(h.el('dtmf-new-code').value,'');assert.equal(h.el('dtmf-admin-password').value,'');
    }
  });
  await check('Confirmed password form refreshes state, sends current admin reauth and clears every secret input',async()=>{
    const h=harness();h.el('dtmf-new-code').value='ABCD12';h.el('dtmf-admin-password').value='current-admin';
    await h.el('dtmf-code-form').dispatch('submit');assert.equal(h.calls[0].path,'/api/status');assert.equal(h.posts().length,1);
    const post=h.posts()[0];assert.equal(post.path,'/api/dtmf-password');assert.equal(post.options.headers['X-CSRF-Token'],'token-1');
    assert.deepEqual(JSON.parse(post.options.body),{code:'ABCD12',admin_password:'current-admin'});
    for(const id of['dtmf-new-code','dtmf-admin-password'])assert.equal(h.el(id).value,'');
    assert.equal(h.el('dtmf-change-code').disabled,true);assert.equal(h.el('dtmf-enabled').disabled,true);assert.equal(h.el('software-check').disabled,true);
    assert.equal(h.el('radio-inspect-rx').disabled,true);assert.equal(h.el('settings-fields').disabled,true);
  });
  for(const [label,change] of[
    ['helper unavailable',h=>{h.status.dtmf_control.code_change.available=false;}],
    ['another password request',h=>{h.status.dtmf_control.code_change.pending=true;}],
    ['unfinished radio',h=>{h.status.radio_programming.roles.rx.verification_required=true;}],
    ['admin token',h=>{h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'changed'});}],
    ['sign-out',h=>{h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});}]
  ])await check(`Changed ${label} during password review clears secret inputs and prevents a stale request`,async()=>{
    const h=harness();h.el('dtmf-new-code').value='ABCD12';h.el('dtmf-admin-password').value='current-admin';
    h.onStatus=()=>change(h);await h.el('dtmf-code-form').dispatch('submit');assert.equal(h.posts().length,0);
    for(const id of['dtmf-new-code','dtmf-admin-password'])assert.equal(h.el(id).value,'');
  });
  await check('Failed status refresh never sends password change or retains fields',async()=>{
    const h=harness();h.el('dtmf-new-code').value='ABCD12';h.el('dtmf-admin-password').value='current-admin';
    h.failRefresh=true;await h.el('dtmf-code-form').dispatch('submit');assert.equal(h.posts().length,0);assert.equal(h.el('dtmf-admin-password').value,'');
  });
  await check('Sign-out or session-token replacement erases a partially filled DTMF password form',async()=>{
    const h=harness();for(const id of['dtmf-new-code','dtmf-admin-password'])h.el(id).value='secret';
    h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'changed'});
    for(const id of['dtmf-new-code','dtmf-admin-password'])assert.equal(h.el(id).value,'');
  });
  await check('Password completion polling restores controls and renders only its public result',async()=>{
    const h=harness();h.status.dtmf_control.code_change={available:true,pending:true,request_id:'9'.repeat(32),success:null,message:'Changing the private DTMF password.'};h.ctx.testApi.status(h.status);
    assert.equal(h.el('dtmf-new-code').disabled,true);assert.equal(h.el('dtmf-announcements').disabled,true);
    Object.assign(h.status.dtmf_control.code_change,{pending:false,success:true,message:'DTMF password changed. Authenticate again over the radio.'});h.ctx.testApi.status(h.status);
    assert.equal(h.el('dtmf-new-code').disabled,false);assert.match(h.el('dtmf-code-message').textContent,/password changed/);
  });
  await check('Private labels through the schema limit remain selectable and longer labels are rejected',async()=>{
    for(const size of[60,61,80,81]){
      const h=pairHarness();h.status.radio_programming.pair_switching.presets[1].label='L'.repeat(size);h.ctx.testApi.status(h.status);
      assert.equal(h.el('radio-pair-select').children.some(option=>option.value==='rpt1'),size<=80);
    }
  });
  await check('Missing private catalog disables programming and pairs while keeping read-only inspection available',async()=>{
    const h=pairHarness({radios:{catalog_configured:false,catalog_digest:null,plan:null,pair_switching:{enabled:false,presets:[]}}});
    assert.equal(h.el('radio-program-rx').disabled,true);assert.equal(h.el('radio-pair-change').disabled,true);
    assert.equal(h.el('program-rx-frequency').textContent,'Not configured');assert.equal(h.el('radio-inspect-rx').disabled,false);
    await h.el('radio-program-rx').dispatch('click');assert.equal(h.posts().length,0);
    await h.el('radio-inspect-rx').dispatch('click');await h.el('radio-confirm-apply').dispatch('click');
    assert.deepEqual(JSON.parse(h.posts()[0].options.body),{role:'rx',action:'inspect'});
  });
  await check('Changed catalog invalidates a reviewed program even when the default channel values match',async()=>{
    const h=harness();await h.el('radio-program-rx').dispatch('click');
    h.onStatus=()=>{h.status.radio_programming.catalog_digest='e'.repeat(64);};await h.el('radio-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,0);assert.match(h.el('radio-programming-message').textContent,/changed/);
  });
  await check('Changed catalog invalidates a reviewed pair and clears its fresh password',async()=>{
    const h=pairHarness();await openPair(h);h.el('radio-pair-confirm-password').value='fresh-password';
    h.onStatus=()=>{h.status.radio_programming.catalog_digest='e'.repeat(64);};await h.el('radio-pair-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,0);assert.equal(h.el('radio-pair-confirm-password').value,'');
  });
  await check('A pending pair tied to a changed catalog cannot start a replacement transaction',async()=>{
    const h=pairHarness();Object.assign(h.status.radio_programming.pair,{catalog_changed:true,verification_required:true});
    h.status.radio_programming.roles.rx.verification_required=true;h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-pair-change').disabled,true);assert.match(h.el('radio-pair-lock-note').textContent,/Restore.*private catalog/);assert.equal(h.posts().length,0);
  });
  await check('A migrated completed pair asks for new verification without claiming work is running',async()=>{
    const h=pairHarness();Object.assign(h.status.radio_programming.pair,{catalog_changed:true,stored_verified:false,plan:null});
    h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-pair-state').textContent,'Catalog changed');
    assert.equal(h.el('radio-pair-current').textContent,'New pair verification needed');
    assert.match(h.el('radio-pair-current-detail').textContent,/private channel catalog changed.*new paired programming task/);
    assert.match(h.el('radio-pair-message').textContent,/private channel catalog changed/);
    assert.ok(!/Requested pair|Wait for both|audio stays paused/.test(h.el('radio-pair-current-detail').textContent));
    assert.equal(h.el('radio-pair-stages').hidden,true);
    assert.equal(h.el('radio-pair-change').disabled,false);
    assert.equal(h.posts().length,0);
  });
  await check('An active paired job takes precedence over the previous catalog record',async()=>{
    const h=pairHarness();Object.assign(h.status.radio_programming,{pending:true,action:'change-pair',success:null});
    Object.assign(h.status.radio_programming.pair,{catalog_changed:true,stored_verified:false,plan:null});
    h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-pair-state').textContent,'Changing pair');
    assert.match(h.el('radio-pair-message').textContent,/Programming and verifying RX, then TX/);
    assert.equal(h.el('radio-pair-stages').hidden,false);
    assert.equal(h.el('radio-pair-change').disabled,true);
  });
  await check('Incomplete or failed pairs retain recovery guidance instead of the neutral catalog notice',async()=>{
    for(const unfinished of[true,false]){
      const h=pairHarness();Object.assign(h.status.radio_programming,{action:'change-pair',success:unfinished?null:false});
      Object.assign(h.status.radio_programming.pair,{catalog_changed:true,stored_verified:false,verification_required:unfinished,phase:'failed',plan:null});
      h.ctx.testApi.status(h.status);
      assert.equal(h.el('radio-pair-state').textContent,'Pair incomplete');
      assert.match(h.el('radio-pair-message').textContent,/operation is incomplete.*audio remains paused/);
      assert.match(h.el('radio-pair-current-detail').textContent,/pair is incomplete.*Review the activity log/);
      assert.equal(h.el('radio-pair-stages').hidden,false);
    }
  });
  await check('Pair switching shows saved frequencies and opens a review without sending a write',async()=>{
    const h=pairHarness();
    assert.match(h.el('radio-pair-current').textContent,/145\.300.*433\.500/);
    await openPair(h);
    assert.equal(h.el('radio-pair-confirm-dialog').open,true);
    assert.match(h.el('radio-pair-confirm-old').textContent,/145\.300.*433\.500/);
    assert.match(h.el('radio-pair-confirm-new').textContent,/145\.330.*433\.525/);
    assert.equal(h.el('radio-pair-confirm-password').value,'');
    assert.equal(h.posts().length,0);
  });
  await check('Pair confirmation requires a newly entered admin password and preserves the displayed pair after an empty submission',async()=>{
    const h=pairHarness();await openPair(h);await confirmPair(h,'');
    assert.equal(h.posts().length,0);
    assert.match(h.el('radio-pair-current').textContent,/145\.300.*433\.500/);
    assert.equal(h.el('radio-pair-confirm-dialog').open,true);
    assert.equal(h.el('radio-pair-confirm-apply').disabled,true);
  });
  await check('Pair Cancel clears the password and a forced stale confirmation sends nothing',async()=>{
    const h=pairHarness();await openPair(h);
    h.el('radio-pair-confirm-password').value='do-not-retain';
    await h.el('radio-pair-confirm-cancel').dispatch('click');
    assert.equal(h.el('radio-pair-confirm-dialog').open,false);
    assert.equal(h.el('radio-pair-confirm-password').value,'');
    await h.el('radio-pair-confirm-apply').dispatch('click');
    assert.equal(h.posts().length,0);
  });
  await check('Pair Escape and native dialog close erase the password',async()=>{
    for(const nativeClose of[false,true]){
      const h=pairHarness();await openPair(h);h.el('radio-pair-confirm-password').value='do-not-retain';
      if(nativeClose)h.el('radio-pair-confirm-dialog').close();
      else {const event=await h.el('radio-pair-confirm-dialog').dispatch('cancel');assert.equal(event.defaultPrevented,true);}
      assert.equal(h.el('radio-pair-confirm-password').value,'');
      assert.equal(h.el('radio-pair-confirm-dialog').open,false);
      await h.el('radio-pair-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
    }
  });
  await check('Confirmed pair switching sends only pair ID and password with CSRF after fresh status',async()=>{
    const h=pairHarness();await openPair(h);await confirmPair(h,'fresh-password');
    assert.equal(h.posts().length,1);
    const post=h.posts()[0];assert.equal(post.path,'/api/radio-pair');
    assert.deepEqual(JSON.parse(post.options.body),{pair_id:'rpt1',password:'fresh-password',catalog_digest:'c'.repeat(64)});
    assert.equal(post.options.headers['X-CSRF-Token'],'token-1');
    assert.equal(h.calls[0].path,'/api/status');
    assert.equal(h.el('radio-pair-confirm-password').value,'');
    assert.equal(h.el('radio-pair-confirm-dialog').open,false);
    for(const id of['radio-pair-select','radio-pair-change','radio-program-rx','radio-program-tx','software-check','software-install','settings-fields','recording-toggle','wifi-visibility-fields'])assert.equal(h.el(id).disabled,true,id);
  });
  await check('Incorrect pair password stays visible as an error without retaining the password or changing the displayed pair',async()=>{
    const h=pairHarness();h.pairError={status:400,message:'Incorrect admin password.'};
    await openPair(h);await confirmPair(h,'wrong-password');
    assert.equal(h.posts().length,1);
    assert.equal(h.el('radio-pair-confirm-password').value,'');
    assert.match(h.el('radio-pair-message').textContent,/Incorrect admin password/);
    assert.match(h.el('radio-pair-current').textContent,/145\.300.*433\.500/);
    assert.ok(!h.el('radio-pair-message').textContent.includes('wrong-password'));
  });
  for(const[name,change]of[
    ['Pair preset frequencies changed',h=>{h.status.radio_programming.pair_switching.presets[1].output_hz=433550000;}],
    ['Selected pair was removed',h=>{h.status.radio_programming.pair_switching.presets=h.status.radio_programming.pair_switching.presets.filter(item=>item.id!=='rpt1');}],
    ['Pair qualification was disabled',h=>{h.status.radio_programming.pair_switching.enabled=false;}],
    ['Saved pair changed',h=>{h.status.radio_programming.pair.pair_id='rpt3';h.status.radio_programming.pair.request_id='e'.repeat(32);}],
    ['Radio request changed',h=>{h.status.radio_programming.request_id='new-radio-task';}],
    ['RX identity changed',h=>{h.status.radio_programming.roles.rx.identity='different-rx';}],
    ['TX firmware changed',h=>{h.status.radio_programming.roles.tx.firmware='different-firmware';}],
    ['Radio compatibility changed',h=>{h.status.radio_programming.roles.tx.compatible=false;}],
    ['Radio helper became unavailable',h=>{h.status.radio_programming.available=false;}],
    ['Radio configuration changed',h=>{h.status.radio_programming.configured=false;}],
    ['Radio work became pending',h=>{h.status.radio_programming.pending=true;}],
    ['Software work became pending',h=>{h.status.software_updates.pending=true;}],
    ['Wi-Fi work became pending',h=>{h.status.wifi_visibility.pending=true;}],
    ['Unrelated verification became necessary',h=>{h.status.radio_programming.roles.programmer={verification_required:true};}],
    ['Admin signed out',h=>{h.status.admin.authenticated=false;}],
    ['Admin CSRF token changed',h=>{h.onStatus=()=>h.ctx.testApi.session({configured:true,authenticated:true,csrf_token:'token-2'});}]
  ])await check(`${name} prevents a stale paired write and clears its password`,async()=>{
    const h=pairHarness();await openPair(h);change(h);await confirmPair(h,'fresh-password');
    assert.equal(h.posts().length,0);
    assert.equal(h.el('radio-pair-confirm-password').value,'');
    assert.equal(h.el('radio-pair-confirm-dialog').open,false);
    assert.match(h.el('radio-pair-message').textContent,/changed|unavailable|finish|wait|verify/i);
  });
  await check('Failed pair status refresh and cancellation during refresh never queue a request or keep the password',async()=>{
    for(const cancel of[false,true]){
      const h=pairHarness();await openPair(h);
      if(cancel)h.onStatus=()=>h.el('radio-pair-confirm-cancel').dispatch('click');else h.failRefresh=true;
      await confirmPair(h,'fresh-password');
      assert.equal(h.posts().length,0);
      assert.equal(h.el('radio-pair-confirm-password').value,'');
      assert.equal(h.el('radio-pair-confirm-dialog').open,false);
    }
  });
  await check('Preview, missing qualification, incomplete setup and signed-out state cannot open or force a paired write',async()=>{
    for(const options of[
      {preview:true},
      {radios:{pair_switching:{enabled:false,presets:[],message:'Bench qualification required.'}}},
      {radios:{pair_switching:undefined}},
      {radios:{configured:false}},
      {radios:{available:false}},
      {signedOut:true}
    ]){
      const h=pairHarness(options);
      if(options.signedOut)h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});
      assert.equal(h.el('radio-pair-change').disabled,true);
      await openPair(h);await confirmPair(h,'fresh-password');
      assert.equal(h.el('radio-pair-confirm-dialog').open,false);assert.equal(h.posts().length,0);
    }
  });
  await check('Unknown pair selections cannot create a confirmation or POST',async()=>{
    const h=pairHarness();await openPair(h,'not-a-preset');
    assert.equal(h.el('radio-pair-confirm-dialog').open,false);await confirmPair(h,'fresh-password');assert.equal(h.posts().length,0);
  });
  await check('Malformed pair metadata cannot authorize a confirmed write',async()=>{
    for(const mutate of[
      preset=>{preset.id='../../unsafe';},
      preset=>{preset.input_hz='145.330';},
      preset=>{preset.output_hz=0;},
      preset=>{delete preset.input_hz;}
    ]){
      const h=pairHarness();mutate(h.status.radio_programming.pair_switching.presets[1]);
      h.ctx.testApi.status(h.status);await openPair(h,h.status.radio_programming.pair_switching.presets[1].id);
      await confirmPair(h,'fresh-password');assert.equal(h.posts().length,0);
    }
  });
  await check('Signing out with a pair review open erases the password and prevents submission',async()=>{
    const h=pairHarness();await openPair(h);h.el('radio-pair-confirm-password').value='do-not-retain';
    h.ctx.testApi.session({configured:true,authenticated:false,csrf_token:null});
    assert.equal(h.el('radio-pair-confirm-password').value,'');
    assert.equal(h.el('radio-pair-confirm-dialog').open,false);
    await h.el('radio-pair-confirm-apply').dispatch('click');assert.equal(h.posts().length,0);
  });
  await check('Pair retry requires a recorded partial pair and a fresh password while ordinary unfinished uploads block it',async()=>{
    const ordinary=pairHarness();ordinary.status.radio_programming.roles.rx.verification_required=true;
    ordinary.ctx.testApi.status(ordinary.status);assert.equal(ordinary.el('radio-pair-change').disabled,true);
    await openPair(ordinary);await confirmPair(ordinary,'fresh-password');assert.equal(ordinary.posts().length,0);
    const h=pairHarness();h.status.radio_programming.roles.rx.verification_required=true;
    Object.assign(h.status.radio_programming.pair,{verification_required:true,stored_verified:false,phase:'failed',roles:{rx:{written:true,verified:false},tx:{written:false,verified:false}}});
    h.ctx.testApi.status(h.status);assert.equal(h.el('radio-pair-change').disabled,false);
    await openPair(h);await confirmPair(h,'');assert.equal(h.posts().length,0);
    await confirmPair(h,'fresh-password');assert.equal(h.posts().length,1);
    assert.equal(JSON.parse(h.posts()[0].options.body).password,'fresh-password');
  });
  await check('CSRF or authentication rejection signs out and still erases a paired confirmation password',async()=>{
    const h=pairHarness();h.pairError={status:403,message:'Admin authentication expired.'};
    await openPair(h);await confirmPair(h,'fresh-password');
    assert.equal(h.posts().length,1);assert.equal(h.el('radio-pair-confirm-password').value,'');
    assert.equal(h.el('radio-pair-confirm-dialog').open,false);
    assert.equal(h.el('radio-pair-change').disabled,true);
    assert.match(h.el('radio-pair-message').textContent,/authentication expired/i);
  });
  await check('A pair request in flight interlocks a forced second pair confirmation and other maintenance',async()=>{
    const h=pairHarness();const originalFetch=h.ctx.fetch;let release;
    h.ctx.fetch=async(path,options)=>path==='/api/radio-pair'?await new Promise(resolve=>{release=async()=>resolve(await originalFetch(path,options));}):await originalFetch(path,options);
    await openPair(h);const first=confirmPair(h,'fresh-password');
    for(let tick=0;tick<30&&!release;tick++)await Promise.resolve();
    assert.equal(typeof release,'function');
    for(const id of['radio-pair-change','radio-program-rx','software-check','settings-fields'])assert.equal(h.el(id).disabled,true,id);
    await h.el('radio-pair-confirm-apply').dispatch('click');await h.el('software-check').dispatch('click');
    await release();await first;
    assert.equal(h.posts().length,1);assert.equal(h.posts()[0].path,'/api/radio-pair');
    assert.equal(h.el('radio-pair-confirm-password').value,'');
  });
  await check('Paired progress safely renders both radio stages without a single role and retains escaped activity text',async()=>{
    const h=pairHarness();Object.assign(h.status.radio_programming,{pending:true,action:'change-pair',phase:'writing-tx',progress:{current:32,total:128,percent:25,unit:'blocks'},events:[{time:'2026-10-29T00:00:00Z',message:'<b>TX write</b>'}]});
    delete h.status.radio_programming.role;
    Object.assign(h.status.radio_programming.pair,{phase:'writing-tx',stored_verified:false,roles:{rx:{written:true,verified:true},tx:{written:false,verified:false}}});
    h.ctx.testApi.status(h.status);
    assert.equal(h.el('radio-progress-panel').hidden,false);
    assert.match(h.el('radio-progress-phase').textContent,/TX/);
    assert.ok(!h.el('radio-progress-phase').textContent.includes('undefined'));
    assert.match(h.el('radio-progress-events').textContent,/<b>TX write<\/b>/);
    assert.match(h.el('radio-pair-stage-rx').textContent,/verified/i);
    assert.ok(!/verified/i.test(h.el('radio-pair-stage-tx').textContent));
    assert.equal(h.el('radio-pair-change').disabled,true);
  });
  await check('Stored pair verification does not silently present pending activation as a live frequency change',async()=>{
    const h=pairHarness();Object.assign(h.status.radio_programming.pair,{stored_verified:true,activation_required:true});
    h.ctx.testApi.status(h.status);
    assert.match(`${h.el('radio-pair-current-detail').textContent} ${h.el('radio-pair-message').textContent}`,/activat|power.cycle|select.*memory/i);
    assert.equal(h.posts().length,0);
  });
  console.log(JSON.stringify({passed:checks.length,checks},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
