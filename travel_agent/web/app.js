'use strict';
const $ = (id) => document.getElementById(id);
const state = {config:null, trip:null, proposal:null, busy:false, events:[], selectedInterests:new Set(),
  origin:null, destination:null, map:null, layer:null, selecting:null, stream:null, command:null, flowerRun:null, jobId:null,
  locatedCity:null, locatedPlace:null, locating:false, autoTitle:null,
  brief:{}, chatHistory:[], chatBusy:false};
const ROLES = {
  discovery: ['Discovery', 'Places and personal interests', '⌕'],
  conditions: ['Conditions', 'Weather and activity exposure', '☂'],
  mobility: ['Mobility', 'Routes and travel intervals', '↗'],
  budget_pace: ['Budget & pace', 'Cost, walking and breaks', '≋']
};
const TYPES = ['art','architecture','parks','coffee','history','food','books','shopping'];
// Plain words for the specialists' read-only look-ups (tool.completed.name) and for the validation status words.
const TOOL_LABELS = {search_places:'searched the place catalog for your interests', get_place_details:'looked up one place',
  get_weather_forecast:'checked the weather forecast', get_route_matrix:'checked travel times between the candidate places',
  assess_costs:'added up the costs', validate_itinerary:'checked the draft schedule against your constraints'};
const STATUS_HELP = {valid:'Valid: every stop, route and constraint checks out.',
  provisional:'Provisional: the schedule fits your constraints, but some prices, hours or weather values are unverified.',
  infeasible:'Infeasible: no schedule satisfies every constraint, so nothing changes until one is relaxed.'};
function node(tag, className, text) {
  const n=document.createElement(tag); if(className)n.className=className;
  if(text!==undefined)n.textContent=text; return n;
}
function button(text, className, fn) {const b=node('button',className,text);b.type='button';b.onclick=fn;return b;}
function toast(message,error=false,warning=false) {
  const n=$('toast');n.textContent=message;n.classList.toggle('error',error);n.classList.toggle('warning',warning&&!error);n.hidden=false;
  clearTimeout(toast.timer);toast.timer=setTimeout(()=>n.hidden=true,7000);
}
async function api(path, options={}) {
  let response;
  try{response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});}
  catch{throw new Error('The server could not be reached. Check that it is still running, then try again.');}
  let data;
  try{data=await response.json();}catch{data={detail:`The server returned an unreadable response (HTTP ${response.status}).`};}
  if(!response.ok) {
    if(response.status===401 && !$('login-dialog').open)$('login-dialog').showModal();
    const message=typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data);
    const error=new Error(message);error.status=response.status;throw error;
  }
  return data;
}
const post=(path,body={})=>api(path,{method:'POST',body:JSON.stringify(body)});
function view() {return state.proposal?state.proposal.proposed:state.trip;}
function time(iso) {
  if(!iso)return '--:--';
  return new Intl.DateTimeFormat('en-GB',{timeZone:view()?.request.timezone||$('timezone').value,
    hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(iso));
}
function dateAt(iso,tz) {
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:tz,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(iso));
  const p=Object.fromEntries(parts.map(x=>[x.type,x.value]));return `${p.year}-${p.month}-${p.day}`;
}
function zonedISO(date, clock, zone) {
  const [y,m,d]=date.split('-').map(Number),[h,min]=clock.split(':').map(Number);
  const wanted=Date.UTC(y,m-1,d,h,min);let guessed=wanted;
  const fmt=new Intl.DateTimeFormat('en-GB',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  for(let i=0;i<3;i++) {
    const p=Object.fromEntries(fmt.formatToParts(new Date(guessed)).map(x=>[x.type,x.value]));
    const actual=Date.UTC(+p.year,+p.month-1,+p.day,+p.hour,+p.minute,+p.second);
    guessed+=wanted-actual;
  }
  return new Date(guessed).toISOString();
}
function formatMoney(minor,currency) {return `${(minor/100).toFixed(2)} ${currency}`;}
function formatMetrics(m) {
  // "<model_calls> model calls · <tool_calls> tool calls · <provider_calls> provider calls · <elapsed>s"; absent counts are omitted.
  const parts=[];
  for(const [key,label] of [['model_calls','model calls'],['tool_calls','tool calls'],['provider_calls','provider calls']])
    if(typeof m?.[key]==='number')parts.push(`${m[key]} ${label}`);
  if(typeof m?.elapsed_s==='number')parts.push(`${m.elapsed_s}s`);
  return parts.join(' · ');
}
function roleName(role) {return ROLES[role]?.[0]||role;}
function interestButtons() {
  $('interests').replaceChildren();
  for(const t of TYPES) {
    const b=button(t[0].toUpperCase()+t.slice(1),'interest-chip',()=>{
      if(state.selectedInterests.has(t))state.selectedInterests.delete(t);else state.selectedInterests.add(t);interestButtons();
    });
    b.classList.toggle('active',state.selectedInterests.has(t));b.setAttribute('aria-pressed',state.selectedInterests.has(t));
    $('interests').append(b);
  }
}
function syncForm(req) {
  $('trip-title').value=req.title;$('city').value=req.city;$('timezone').value=req.timezone;
  $('trip-date').value=dateAt(req.start,req.timezone);
  const fmt=new Intl.DateTimeFormat('en-GB',{timeZone:req.timezone,hour:'2-digit',minute:'2-digit',hour12:false});
  $('start-time').value=fmt.format(new Date(req.start));$('end-time').value=fmt.format(new Date(req.end));
  $('budget').value=req.budget_minor/100;$('walking').value=req.max_walking_m/1000;
  $('transport').value=req.transport_mode;$('stops').value=req.target_stops;$('avoid-rain').checked=req.avoid_rain_outdoor_visits;
  state.origin=req.origin;state.destination=req.destination;state.selectedInterests=new Set(req.interests);
  state.locatedCity=req.city;state.locatedPlace=null;
  interestButtons();updateRangeLabels();updateDetailsMeta();
}
function updateRangeLabels() {
  $('budget-label').textContent=`${state.trip?.request.currency||'EUR'} ${$('budget').value}`;
  $('walking-label').textContent=`${Number($('walking').value).toFixed(1)} km`;
}
function updateDetailsMeta() {
  // The collapsed <details> summary still tells the user what the form holds, so the direct path is never hidden.
  $('details-meta').textContent=`${$('city').value||'—'} · ${$('trip-date').value||'no date'} · ${$('start-time').value}–${$('end-time').value}`;
}
function buildRequest() {
  const base=structuredClone(state.config.default_request);
  return {...base,title:$('trip-title').value,city:$('city').value,timezone:$('timezone').value,
    start:zonedISO($('trip-date').value,$('start-time').value,$('timezone').value),
    end:zonedISO($('trip-date').value,$('end-time').value,$('timezone').value),
    origin:state.origin,destination:state.destination,interests:[...state.selectedInterests],
    budget_minor:Math.round(Number($('budget').value)*100),max_walking_m:Math.round(Number($('walking').value)*1000),
    transport_mode:$('transport').value,target_stops:+$('stops').value,avoid_rain_outdoor_visits:$('avoid-rain').checked};
}
function briefToRequestFields(brief) {
  // A partial brief layered onto the current form: only the fields the conversation actually set are overwritten.
  const merged=buildRequest();
  const tz=brief.timezone||merged.timezone;
  if(brief.city)merged.city=brief.city;
  if(brief.title)merged.title=brief.title;
  if(brief.timezone)merged.timezone=brief.timezone;
  const date=brief.date||$('trip-date').value;
  if(date&&brief.start_time)merged.start=zonedISO(date,brief.start_time,tz);
  if(date&&brief.end_time)merged.end=zonedISO(date,brief.end_time,tz);
  if(brief.date&&!brief.start_time)merged.start=zonedISO(brief.date,$('start-time').value,tz);
  if(brief.date&&!brief.end_time)merged.end=zonedISO(brief.date,$('end-time').value,tz);
  if(brief.budget_minor!=null)merged.budget_minor=brief.budget_minor;
  if(brief.max_walking_m!=null)merged.max_walking_m=brief.max_walking_m;
  if(brief.transport_mode)merged.transport_mode=brief.transport_mode;
  if(brief.target_stops!=null)merged.target_stops=brief.target_stops;
  if(brief.avoid_rain_outdoor_visits!=null)merged.avoid_rain_outdoor_visits=brief.avoid_rain_outdoor_visits;
  if(brief.interests&&brief.interests.length)merged.interests=[...brief.interests];
  return merged;
}
function busy(value,label='Planning') {
  state.busy=value;$('locate-button').disabled=value;
  const pending=!value&&!!state.proposal,hasTrip=!!state.trip?.itinerary;
  // One primary action per state: Build (no trip) / Update preferences (trip) / review the proposal (pending).
  $('build-button').disabled=value||pending;
  $('build-button').title=pending?'Apply or keep the current itinerary in Changes & evidence first.':'';
  // The button itself says that planning is running; the collaboration panel may be off-screen.
  $('build-label').textContent=value?'Planning…':hasTrip?'Update preferences':'Build itinerary';
  $('job-state').textContent=value?label:pending?'Review pending':'Ready';$('job-state').classList.toggle('busy',value||pending);
  for(const id of ['rain','less-walking','refresh'])$(id).disabled=value||!hasTrip||pending;
  $('scenario-hint').textContent=value?'Specialists are working on a revision. Controls unlock when it is ready.':
    pending?'A proposed revision is waiting below in Changes & evidence.':
    hasTrip?'Try a change: each button proposes a revised plan that you apply or keep the current one.':'Build an itinerary first. Scenarios then propose reviewed revisions.';
}
function initAgents() {
  $('agent-list').replaceChildren();
  for(const [role,[name,desc,icon]]of Object.entries(ROLES)) {
    const row=node('div','agent-card');row.id='agent-'+role;
    row.append(node('div','agent-avatar',icon));const info=node('div','agent-info');
    info.append(node('strong','',name),node('small','',desc));row.append(info,node('div','agent-dot'));$('agent-list').append(row);
  }
}
function addEvent(event) {
  state.events.push(event);const d=event.data;
  if(event.type==='agent.started') {
    const n=$('agent-'+d.role);if(n){n.classList.add('running');n.classList.remove('done');}
  }
  if(event.type==='agent.completed') {
    const n=$('agent-'+d.role);if(n){n.classList.remove('running');n.classList.add('done');}
    const item=node('div','feed-event');item.append(node('strong','',roleName(d.role)),node('p','',d.report.summary));$('event-feed').append(item);
  }
  if(event.type==='collaboration.message' && (d.kind==='request' || d.recipient==='user')) {
    const item=node('div','feed-event handoff');item.append(node('strong','',`${d.sender.toUpperCase()} > ${d.recipient.toUpperCase()}`),node('p','',d.summary));$('event-feed').append(item);
  }
  if(event.type==='agent.completed'&&d.report) {
    // evidence_ids are already pruned server-side; unknown_evidence_ids lists the refs that were dropped.
    const dropped=Array.isArray(d.unknown_evidence_ids)?d.unknown_evidence_ids.length:0;
    let meta=`${d.report.candidate_ids.length} ranked · ${d.report.avoid_ids.length} to avoid · ${(d.report.evidence_ids||[]).length} evidence refs`;
    if(dropped)meta+=` · ${dropped} unverifiable dropped`;
    $('event-feed').lastElementChild?.append(node('small','feed-meta',meta));
  }
  if(event.type==='model.completed') {
    const u=d.usage;let timing=`${d.returned_model||d.requested_model} answered in ${d.elapsed_s}s`;
    if(u&&typeof u.input_tokens==='number'&&typeof u.output_tokens==='number')timing+=` · ${u.input_tokens} in / ${u.output_tokens} out tokens`;
    const item=node('div','feed-event model');item.append(node('strong','',`${roleName(d.role)} · model call ${d.call_number}`),node('p','',timing));$('event-feed').append(item);
  }
  if(event.type==='model.incomplete') {
    const item=node('div','feed-event model');item.append(node('strong','',`${roleName(d.role)} · output truncated at ${d.max_output_tokens} tokens`),node('p','',`Model call ${d.call_number} reached the output cap. The answer may be incomplete; a repair turn can follow.`));$('event-feed').append(item);
  }
  if(event.type==='planner.fallback') {
    const item=node('div','feed-event handoff');item.append(node('strong','',`Coordinator · catalog fallback: ${d.reason}`),node('p','',`${d.candidate_count} candidates taken in catalog order instead of a specialist ranking.`));$('event-feed').append(item);
  }
  if(event.type==='planner.repair') {
    // The Budget & Pace alternative is scored against the same baseline as the proposal; the verdict is disclosed either way.
    const item=node('div','feed-event handoff');item.append(node('strong','',`Coordinator · repair ${d.adopted?'adopted':'declined'}: ${d.reason}`),node('p','',`Proposal scored ${d.proposal_score}, alternative ${d.alternative_score}.`));$('event-feed').append(item);
  }
  if(event.type==='flower.stream.reconnecting') {
    const item=node('div','feed-event');item.append(node('strong','','Flower SuperGrid · event stream reconnecting'),node('p','',`Attempt ${d.attempt} in ${d.delay_s}s after event ${d.after_task_event_id??'none'} (${d.error}). Relayed events are kept.`));$('event-feed').append(item);
  }
  if(event.type==='model.repair') {
    const item=node('div','feed-event');item.append(node('strong','',`${roleName(d.role)} · repair turn`),node('p','',d.reason));$('event-feed').append(item);
  }
  if(event.type==='tool.completed'||event.type==='tool.failed') {
    // Plain words for the read-only look-ups; the raw tool name stays in the tooltip for anyone comparing with the event stream.
    const failed=event.type==='tool.failed',what=TOOL_LABELS[d.name]||`read ${String(d.name).replaceAll('_',' ')}`;
    const item=node('div','feed-event tool');item.title=`tool ${d.name}`;
    item.append(node('strong','',`${roleName(d.role)} ${failed?'could not use a tool':what}`),node('p','',failed?`Rejected: ${d.error}`:'A read-only look-up. Nothing is booked or changed.'));$('event-feed').append(item);
  }
  if(event.type==='evidence.loaded') {
    const item=node('div','feed-event');item.append(node('strong','','Evidence loaded'),node('p','',`${d.place_count} candidate places · weather ${d.weather_status} · ${d.data_mode} data`));$('event-feed').append(item);
  }
  if(event.type==='validation.completed') {
    const item=node('div','feed-event');item.append(node('strong','','Schedule check · Deterministic validation'),node('p','',`${STATUS_HELP[d.status]||d.status} ${d.issues.length} note(s) are listed under Changes & evidence.`));$('event-feed').append(item);
  }
  if(event.type==='flower.submitting') {
    $('flower-panel').hidden=false;$('flower-title').textContent='Planning on SuperGrid';
    $('flower-note').textContent=`Submitting the Flower App Bundle (${d.fab_hash}) to ${d.federation} with ${d.model}.`;
  }
  if(event.type==='flower.run.started') {
    state.flowerRun=d;$('flower-panel').hidden=false;$('flower-run').hidden=false;$('flower-title').textContent='SuperGrid run in progress';
    $('flower-note').textContent='Specialists execute inside a Flower AgentApp. Results are validated again locally before review.';
    $('flower-run').textContent=`Run ${d.run_id} · ${d.federation} · ${d.model}`;
    const item=node('div','feed-event');item.append(node('strong','','Flower SuperGrid'),node('p','',`Run ${d.run_id} started in ${d.federation}${d.note?' · '+d.note:''}`));$('event-feed').append(item);
  }
  if(event.type==='flower.run.finished') {
    $('flower-title').textContent='SuperGrid run completed';
    if(d.metrics)$('flower-run').textContent=`Run ${d.run_id} · ${formatMetrics(d.metrics)} on SuperGrid`;
  }
  if(event.type==='proposal.ready'||event.type==='job.finished') {
    if(d.metrics)$('run-metrics').textContent=formatMetrics(d.metrics);
  }
  if(event.type==='job.failed'){toast(d.error,true);if(state.config.execution_backend==='flower')$('flower-title').textContent='SuperGrid run failed';}
  $('event-feed').scrollTop=$('event-feed').scrollHeight;
}
async function watchJob(result) {
  const job=result.job;state.tripId=result.trip_id;localStorage.setItem('travel-trip',state.tripId);
  // A new job has no SuperGrid run yet: the New-trip notice must never name the run of an earlier, finished job.
  state.flowerRun=null;state.jobId=job.id;
  initAgents();state.events=[];$('event-feed').replaceChildren();$('run-metrics').textContent='Collecting execution measurements...';
  busy(true,job.external?'Awaiting Flower':state.config.execution_backend==='flower'?'SuperGrid':'Planning');
  $('flower-command').hidden=true;$('copy-command').hidden=true;$('flower-run').hidden=true;
  if(job.flower_command) {
    state.command=job.flower_command;$('flower-command').textContent=job.flower_command;$('flower-panel').hidden=false;
    $('flower-title').textContent='Run this planning job';$('flower-note').textContent='Copy this private command into your authenticated project terminal. Its credential is scoped to this job.';
    $('flower-command').hidden=false;$('copy-command').hidden=false;
    toast('Start the scoped Flower command shown in the SuperGrid panel.');
  } else if(state.config.execution_backend==='flower') {
    $('flower-panel').hidden=false;$('flower-title').textContent='Planning on SuperGrid';$('flower-note').textContent='Submitting the job as a Flower AgentApp run.';
  } else $('flower-panel').hidden=true;
  if(state.stream)state.stream.close();
  const source=new EventSource(`/api/jobs/${job.id}/events`);state.stream=source;
  source.onmessage=(event)=>{try{addEvent(JSON.parse(event.data));}catch(e){console.error(e);}};
  let done=false;
  async function finish() {
    if(done)return;done=true;source.close();
    try {
      const [result,finalJob]=await Promise.all([api(`/api/trips/${state.tripId}`),api(`/api/jobs/${job.id}`)]);
      state.trip=result.trip;state.proposal=result.proposals.find(p=>p.id===finalJob.proposal_id)||null;
      if(finalJob.status==='failed')toast(finalJob.error,true);
      if(state.proposal)toast(state.proposal.proposed.itinerary.validation.valid?'Revision ready for review.':'Constraints need attention. The current itinerary is unchanged.');
      syncForm(state.trip.request);render();
      announceResult(finalJob);
    }catch(e){toast(e.message,true);}finally{busy(false);}
  }
  source.addEventListener('done',finish);
  source.onerror=async()=>{
    try{const j=await api(`/api/jobs/${job.id}`);if(!['running','queued'].includes(j.status))await finish();}catch(e){source.close();busy(false);toast(e.message,true);}
  };
}
function announceResult(job) {
  // The outcome of the primary action has to be visible where the user is: the chat thread carries it, the page moves
  // to the itinerary (first plan) or to the review (proposal), and the toast names what happened.
  const chatting=!$('chat-intake').hidden&&state.chatHistory.length>0;
  const jump=(id,label)=>{const chip=button(label,'chat-jump-chip',()=>$(id).scrollIntoView?.({behavior:'smooth',block:'start'}));$('chat-log').append(chip);$('chat-log').scrollTop=$('chat-log').scrollHeight;};
  if(job.status==='failed') {
    if(chatting)appendChatTurn('assistant',`Planning failed: ${job.error} Adjust the details and try again.`,'note');
    return;
  }
  if(state.proposal) {
    if(chatting){appendChatTurn('assistant','A revised plan is ready. Compare it under Changes & evidence, then apply it or keep the current itinerary.','note');jump('review-panel','Show the proposed changes ↓');}
    $('review-panel').scrollIntoView?.({behavior:'smooth',block:'nearest'});
    return;
  }
  const plan=state.trip?.itinerary;if(!plan)return;
  const summary=`${plan.stops.length} stops · ${(plan.walking_m/1000).toFixed(1)} km walking · ${formatMoney(plan.cost_minor,state.trip.request.currency)}`;
  toast(`Itinerary ready: ${summary}.`);
  if(chatting){appendChatTurn('assistant',`Your day in ${state.trip.request.city} is ready: ${summary}. Ask me for changes here, or use the buttons under the itinerary.`,'note');jump('itinerary-panel','Show the itinerary ↓');}
  $('itinerary-panel').scrollIntoView?.({behavior:'smooth',block:'start'});
}
async function recoverFromConflict(e) {
  // 409 means the server already decided this proposal or the itinerary moved on: show the current state, not a dead end.
  if(e.status!==409){toast(e.message,true);return;}
  try{await reloadTrip();}catch(err){toast(err.message,true);return;}
  toast(`${e.message}. Showing the current itinerary (revision ${state.trip?.revision??'?'}) instead.`,true);
}
async function event(kind,payload={},simulated=false) {
  if(!state.trip||state.busy)return;
  if(state.proposal){toast('Apply or reject the pending revision first.');return;}
  try {
    state.flowerRun=null;state.jobId=null;busy(true);
    const body={base_revision:state.trip.revision,event:{id:crypto.randomUUID().replaceAll('-',''),kind,payload,simulated}};
    await watchJob(await post(`/api/trips/${state.trip.id}/events`,body));
  }catch(e){busy(false);await recoverFromConflict(e);}
}
async function reloadTrip() {
  const data=await api(`/api/trips/${state.tripId}`);state.trip=data.trip;state.proposal=null;syncForm(state.trip.request);render();busy(false);
}
function render() {
  const trip=view(), plan=trip?.itinerary;
  $('itinerary-title').textContent=trip?.request.title||(state.locatedPlace?`Explore ${state.locatedPlace.name}, together.`:'Explore Berlin, together.');
  $('revision').textContent=state.proposal?`PROPOSED R${trip.revision}`:trip?`REVISION ${trip.revision}`:'DRAFT';
  // While a proposal is pending the timeline previews it; the heading says so, not only the badge.
  $('timeline-title').textContent=state.proposal?`Proposed timeline · revision ${trip.revision} is not applied yet`:'Your timeline';
  $('metric-stops').textContent=plan?.stops.length||0;
  $('metric-walk').replaceChildren(document.createTextNode(((plan?.walking_m||0)/1000).toFixed(1)+' '),node('small','','km'));
  $('metric-cost').replaceChildren(document.createTextNode(((plan?.cost_minor||0)/100).toFixed(2)+' '),node('small','',trip?.request.currency||'EUR'));
  $('metric-end').textContent=trip?time(plan?.end_arrival||trip.request.end):($('end-time').value||time(state.config?.default_request.end));
  $('export-button').disabled=!state.trip?.itinerary;
  // The same form serves both states; its submit label says what will actually happen.
  $('build-label').textContent=state.trip?.itinerary?'Update preferences':'Build itinerary';
  renderTimeline();renderReview();renderMap();busy(state.busy);
}
function renderTimeline() {
  const trip=view(),plan=trip?.itinerary;if(!plan)return;
  $('timeline').replaceChildren();
  if(!plan.stops.length){$('timeline').append(node('div','empty-timeline','No feasible schedule was found under the current constraints.'));return;}
  plan.stops.forEach((stop,index)=>{
    const leg=plan.legs[index];
    if(leg)$('timeline').append(node('div','transfer-row',`${leg.mode==='walking'?'Walk':'Cycle'} ${Math.ceil(leg.duration_s/60)} min · ${leg.distance_m} m${leg.source_status==='fixture'?' · synthetic route':''}`));
    const card=node('article','timeline-card');
    card.append(node('div','stop-index'+(stop.completed?' completed':''),stop.completed?'✓':String(index+1)));
    const content=node('div');content.append(node('div','stop-time',`${time(stop.start)} to ${time(stop.end)}`));
    const title=node('div','stop-title',stop.name);title.onclick=()=>showPlace(stop.place_id);title.tabIndex=0;title.setAttribute('role','button');
    title.title='Show place details and evidence';title.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();showPlace(stop.place_id);}};
    if(stop.locked)title.append(node('span','stop-tag locked','LOCKED'));
    if(stop.completed)title.append(node('span','stop-tag','COMPLETED'));
    const price=stop.cost_minor===null?'Price unknown':formatMoney(stop.cost_minor,trip.request.currency);
    const place=trip.places.find(p=>p.id===stop.place_id);
    content.append(title,node('div','stop-meta',`${place?.indoor===true?'Indoor':place?.indoor===false?'Outdoor':'Exposure unknown'} · ${Math.round((new Date(stop.end)-new Date(stop.start))/60000)} min · ${price}`));
    card.append(content);const actions=node('div','stop-actions');
    if(!stop.completed&&!state.proposal) {
      actions.append(button(stop.locked?'Unlock':'Lock time','stop-action',()=>event(stop.locked?'unlock_stop':'lock_stop',{place_id:stop.place_id})));
      actions.append(button('Replace','stop-action',()=>event('preferences_changed',{excluded_place_ids:[...state.trip.request.excluded_place_ids,stop.place_id]})));
      if(index===state.trip.progress.completed_place_ids.length)actions.append(button('Complete','stop-action',()=>completeStop(stop)));
    }
    card.append(actions);$('timeline').append(card);
    const pause=plan.breaks.find(b=>b.location_id===stop.place_id&&new Date(b.start)>=new Date(stop.end));
    if(pause)$('timeline').append(node('div','transfer-row',`Break ${time(pause.start)} to ${time(pause.end)} · ${pause.reason}`));
  });
  const last=plan.legs.at(-1);if(last)$('timeline').append(node('div','transfer-row',`${Math.ceil(last.duration_s/60)} min ${last.mode} to finish · arrival ${time(plan.end_arrival)}`));
}
function completeStop(stop) {
  const amount=prompt(`Actual spending at ${stop.name}, in ${state.trip.request.currency}:`,String((stop.cost_minor||0)/100));
  if(amount===null)return;
  if(!/^\d+(\.\d{1,2})?$/.test(amount)){toast('Enter a non-negative amount with at most two decimal places.',true);return;}
  const simulated=state.config.data_mode==='fixture';
  const completed=simulated?stop.end:new Date().toISOString();
  event('stop_completed',{place_id:stop.place_id,actual_spent_minor:Math.round(Number(amount)*100),completed_at:completed},simulated);
}
function renderReview() {
  const trip=view();if(!trip?.itinerary)return;
  const root=$('review-content');root.replaceChildren();const plan=trip.itinerary;
  const summary=node('div','review-summary'+(!plan.validation.valid?' infeasible':''));
  summary.append(node('strong','',state.proposal?'Proposed changes':`Revision ${trip.revision} committed`));
  summary.append(node('div','',`${plan.validation.status[0].toUpperCase()+plan.validation.status.slice(1)} · ${plan.unknown_cost_count} unknown prices`));
  if(STATUS_HELP[plan.validation.status])summary.append(node('small','status-help',STATUS_HELP[plan.validation.status]));root.append(summary);
  if(state.proposal) {
    const names=new Map([...(state.trip?.places||[]),...trip.places].map(p=>[p.id,p.name]));
    const trigger=state.proposal.trigger.replaceAll('_',' ');
    // Only the rain button is a simulation; a preference change made while that scenario is still applied is a real request.
    const simulated=state.proposal.trigger==='rain'&&trip.weather_override?' (simulated scenario)':'';
    root.append(node('p','micro',`Trigger: ${trigger}${simulated}. Coordinator proposal built from ${trip.reports.length} specialist reports.`));
    for(const m of trip.messages.filter(m=>m.kind==='request')) {
      const row=node('div','change-row');row.append(node('span','change-mark',ROLES[m.sender]?.[2]||'·'),node('span','',`${roleName(m.sender)} asked ${roleName(m.recipient)}: ${m.summary}`));root.append(row);
    }
    root.append(node('p','micro diff-legend','− removed · + added · = kept from the current itinerary'));
    for(const [ids,mark,cls]of [[state.proposal.removed,'−','removed'],[state.proposal.added,'+',''],[state.proposal.preserved,'=','']]) {
      ids.forEach(id=>{const row=node('div','change-row');row.append(node('span','change-mark '+cls,mark),node('span','',names.get(id)||id));root.append(row);});
    }
    // Visibility of what Apply actually changes: a proposal with an unchanged stop list still has to say so, and a
    // scenario switch (rain applied, or Refresh conditions clearing it) is the change, not a footnote in the evidence list.
    if(!state.proposal.added.length&&!state.proposal.removed.length)root.append(node('p','micro','No stops are added or removed; timings and evidence are re-checked.'));
    const hadScenario=!!state.trip?.weather_override,hasScenario=!!trip.weather_override;
    if(hadScenario&&!hasScenario)root.append(node('p','micro',`Applying clears the simulated weather scenario; the forecast comes from ${trip.weather?.source.provider||'the configured provider'} again.`));
    if(!hadScenario&&hasScenario)root.append(node('p','micro','Applying keeps the simulated weather scenario on the itinerary until you choose Refresh conditions.'));
    const locks=trip.request.reservations.length;if(locks)root.append(node('p','micro',`${locks} locked reservation(s) protected.`));
    const actions=node('div','review-actions');
    const accept=button('Apply revision','primary',async()=>{try{await post(`/api/proposals/${state.proposal.id}/accept`);await reloadTrip();toast('Revision applied.');}catch(e){await recoverFromConflict(e);}});
    accept.disabled=!plan.validation.valid;
    if(!plan.validation.valid)accept.title='This revision does not satisfy the constraints and cannot be applied.';
    actions.append(accept,button('Keep current','text-button',async()=>{try{await post(`/api/proposals/${state.proposal.id}/reject`);await reloadTrip();toast('Kept the current itinerary.');}catch(e){await recoverFromConflict(e);}}));root.append(actions);
  } else {
    root.append(node('p','micro','Change the weather, walking limit, or a stop to inspect a coordinated revision.'));
    // An applied scenario stays on the trip; say so where the user looks for the state of the plan, with the way out.
    if(trip.weather_override)root.append(node('p','micro','A simulated weather scenario is applied to this revision. Refresh conditions proposes a plan on the regular forecast again.'));
  }
  const details=node('details','evidence-list');details.open=!plan.validation.valid;
  details.append(node('summary','',`Validation and source notes (${plan.validation.issues.length})`));
  const grouped=new Map();plan.validation.issues.forEach(i=>{if(!grouped.has(i.code))grouped.set(i.code,i);});
  for(const issue of grouped.values()){const item=node('div','evidence-item');item.append(node('b','',issue.code.replaceAll('_',' ')),node('div','',issue.message));details.append(item);}
  if(trip.weather){const item=node('div','evidence-item');item.append(node('b','',`${trip.weather.source.provider} · ${trip.weather.source.status}`),node('div','',trip.weather.source.note));details.append(item);}
  root.append(details);
}
function safeLink(url) {try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)?u.href:null;}catch{return null;}}
function showPlace(id) {
  const trip=view(),place=trip?.places.find(p=>p.id===id);if(!place)return;
  const c=$('details-content');c.replaceChildren(node('div','eyebrow','PLACE & EVIDENCE'),node('h2','',place.name),node('p','',place.description));
  const facts=[['Environment',place.indoor===true?'Indoor':place.indoor===false?'Outdoor':'Unknown'],
    ['Price',place.cost_minor===null?'Unknown':`${formatMoney(place.cost_minor,place.currency)} (${place.cost_status})`],
    ['Hours',place.opening_hours||'Unknown'],['Source',`${place.source.provider} (${place.source.status})`],
    ['Coordinates',`${place.coordinate.lat.toFixed(5)}, ${place.coordinate.lon.toFixed(5)}`]];
  for(const [k,v]of facts){const p=node('p');p.append(node('strong','',k+': '),document.createTextNode(v));c.append(p);}
  if(safeLink(place.source.reference)){const a=node('a','text-button','Open source record');a.href=safeLink(place.source.reference);a.target='_blank';a.rel='noopener noreferrer';c.append(a);}
  c.append(node('p','micro',place.source.note));
  if(state.trip&&!state.proposal)c.append(button('Include in itinerary','primary',()=>{$('details-dialog').close();event('preferences_changed',{required_place_ids:[...new Set([...state.trip.request.required_place_ids,id])]});}));
  $('details-dialog').showModal();
}
const SVGNS='http://www.w3.org/2000/svg';
function svg(tag,attrs={},text) {const e=document.createElementNS(SVGNS,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,String(v));if(text!==undefined)e.textContent=text;return e;}
function bounds() {
  // A trip frames its own endpoints (the START marker uses the same ones); a city located for the next trip must not stretch this map.
  const trip=view();
  const origin=trip?(state.trip?.progress.location||trip.request.origin):(state.origin||state.config.default_request.origin);
  const destination=trip?trip.request.destination:(state.destination||state.config.default_request.destination);
  const points=[origin,destination,...(trip?.places||[]).map(p=>p.coordinate)];
  let minLon=Math.min(...points.map(p=>p.lon)),maxLon=Math.max(...points.map(p=>p.lon));
  let minLat=Math.min(...points.map(p=>p.lat)),maxLat=Math.max(...points.map(p=>p.lat));
  const dx=Math.max(maxLon-minLon,.014)*.20,dy=Math.max(maxLat-minLat,.008)*.25;
  if(maxLon===minLon){maxLon+=.007;minLon-=.007;}if(maxLat===minLat){maxLat+=.004;minLat-=.004;}
  return {minLon:minLon-dx,maxLon:maxLon+dx,minLat:minLat-dy,maxLat:maxLat+dy};
}
function renderMap() {
  if(!state.config)return;
  if(state.map){renderLeaflet();return;}
  const s=$('schematic');if(!s)return;s.replaceChildren();const b=bounds(),trip=view();
  const xy=p=>[40+(p.lon-b.minLon)/(b.maxLon-b.minLon)*820,70+(b.maxLat-p.lat)/(b.maxLat-b.minLat)*450];
  state.projection={bounds:b,xy};
  for(let x=0;x<950;x+=65)s.append(svg('line',{x1:x,y1:0,x2:x,y2:590,stroke:'#ece3d3','stroke-width':.8}));
  for(let y=0;y<650;y+=65)s.append(svg('line',{x1:0,y1:y,x2:900,y2:y,stroke:'#ece3d3','stroke-width':.8}));
  s.append(svg('text',{x:860,y:91,fill:'#8a7c6d','font-size':12,'text-anchor':'middle'},'N'),svg('path',{d:'M 860 104 L 854 120 L 860 116 L 866 120 Z',fill:'#8a7c6d'}));
  function route(legs,old=false) {
    for(const l of legs||[]){if(!l.geometry?.length)continue;const pts=l.geometry.map(([lon,lat])=>xy({lat,lon}).join(',')).join(' ');
      s.append(svg('polyline',{points:pts,fill:'none',stroke:old?'#c2ab8a':'#ba5a34','stroke-width':old?5:3,'stroke-linecap':'round','stroke-linejoin':'round','stroke-dasharray':l.source_status==='fixture'?'7 6':'none',opacity:old?.45:.9}));}
  }
  if(state.proposal)route(state.trip?.itinerary?.legs,true);route(trip?.itinerary?.legs);
  const active=new Map((trip?.itinerary?.stops||[]).map((s,i)=>[s.place_id,i+1]));
  for(const p of trip?.places||[]) {
    const [x,y]=xy(p.coordinate),index=active.get(p.id);const g=svg('g',{tabindex:0,role:'button','aria-label':p.name,style:'cursor:pointer'});
    g.onclick=()=>showPlace(p.id);g.onkeydown=e=>{if(e.key==='Enter')showPlace(p.id);};
    if(index){g.append(svg('circle',{cx:x,cy:y,r:21,fill:'#ba5a34',stroke:'#fff','stroke-width':4}));g.append(svg('text',{x,y:y+4,fill:'#fff','font-size':13,'text-anchor':'middle','font-weight':600},index));}
    else g.append(svg('circle',{cx:x,cy:y,r:5,fill:'#c2b8a8',stroke:'#f5efe3','stroke-width':2}));
    const label=p.name.length>25?p.name.slice(0,23)+'..':p.name;
    if(index){g.append(svg('rect',{x:x-82,y:y+25,width:164,height:23,rx:5,fill:'#fffffff5'}));g.append(svg('text',{x,y:y+40,'text-anchor':'middle',fill:'#6b5d4f','font-size':9.5},label));}
    s.append(g);
  }
  const origin=state.trip?.progress.location||trip?.request.origin||state.origin;
  const [ox,oy]=xy(origin);s.append(svg('rect',{x:ox-6,y:oy-6,width:12,height:12,rx:3,fill:'#bd9659',stroke:'white','stroke-width':2}));
  s.append(svg('text',{x:ox+11,y:oy+4,fill:'#906d35','font-size':9,'font-weight':600},'START'));
  if(!trip?.places.length) {
    s.append(svg('text',{x:450,y:272,'text-anchor':'middle',fill:'#a89686','font-size':17,'font-family':'Georgia'},'The route starts with your preferences.'));
    s.append(svg('text',{x:450,y:299,'text-anchor':'middle',fill:'#c2a488','font-size':11},'Build an itinerary to explore candidate places.'));
  }
  s.append(svg('text',{x:25,y:541,fill:'#a89686','font-size':9},`${b.minLat.toFixed(3)} N / ${b.minLon.toFixed(3)} E`));
  $('route-label').textContent=trip?.data_mode==='fixture'?'Synthetic direct-line routes. Not walking directions.':'Simplified map. Load the street map to see roads.';
  s.onclick=e=>{
    if(!state.selecting)return;
    const rect=s.getBoundingClientRect(),x=(e.clientX-rect.left)/rect.width*900,y=(e.clientY-rect.top)/rect.height*590;
    pickLocation({lon:b.minLon+(x-40)/820*(b.maxLon-b.minLon),lat:b.maxLat-(y-70)/450*(b.maxLat-b.minLat)});
  };
}
function renderLeaflet() {
  const L=window.L,trip=view();state.layer.clearLayers();let box=[];
  for(const l of trip?.itinerary?.legs||[]) {
    if(l.geometry.length>1)L.geoJSON({type:'Feature',properties:{},geometry:{type:'LineString',coordinates:l.geometry}},
      {style:{color:'#ba5a34',weight:4,dashArray:l.source_status==='fixture'?'8 7':null}}).addTo(state.layer);
  }
  const active=new Map((trip?.itinerary?.stops||[]).map((s,i)=>[s.place_id,i+1]));
  for(const p of trip?.places||[]) {
    const n=active.get(p.id);const latlng=[p.coordinate.lat,p.coordinate.lon];box.push(latlng);
    const icon=L.divIcon({className:'leaflet-div-icon',html:`<span class="map-marker${n?'':' candidate'}">${n||''}</span>`,iconSize:[30,30],iconAnchor:[15,15]});
    const marker=L.marker(latlng,{icon}).addTo(state.layer);const text=node('div','',p.name);marker.bindTooltip(text);marker.on('click',()=>showPlace(p.id));
  }
  const origin=trip?.progress.location||trip?.request.origin||state.origin;
  L.circleMarker([origin.lat,origin.lon],{radius:6,color:'#ae8345',fillColor:'#ae8345',fillOpacity:1}).addTo(state.layer);
  box.push([origin.lat,origin.lon]);if(box.length>1)state.map.fitBounds(box,{padding:[45,65],maxZoom:15});
  $('route-label').textContent=trip?.data_mode==='fixture'?'Synthetic route overlay on OSM. Not navigation directions.':'Provider route geometry over OpenStreetMap.';
}
async function loadOSM() {
  if(state.map)return;
  $('load-osm').disabled=true;$('load-osm').textContent='Loading...';
  try {
    const css=document.createElement('link');css.rel='stylesheet';css.href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';document.head.append(css);
    await new Promise((resolve,reject)=>{const s=document.createElement('script');s.src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';s.onload=resolve;s.onerror=()=>reject(new Error('Leaflet could not load. The offline schematic remains available.'));document.head.append(s);setTimeout(()=>reject(new Error('Map library request timed out.')),12000);});
    $('map').replaceChildren();const L=window.L;
    state.map=L.map('map',{zoomControl:true}).setView([state.origin.lat,state.origin.lon],14);
    const tile=L.tileLayer(state.config.tile_url,{attribution:'&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',maxZoom:19});
    let tileFailed=false;tile.on('tileerror',()=>{if(!tileFailed){tileFailed=true;toast('A map tile failed to load. Routes and place coordinates remain visible.',true);}});tile.addTo(state.map);
    state.layer=L.layerGroup().addTo(state.map);state.map.on('click',e=>{if(state.selecting)pickLocation({lat:e.latlng.lat,lon:e.latlng.lng});});
    $('map-label').textContent='OPENSTREETMAP';$('load-osm').textContent='Street map loaded';renderLeaflet();
  }catch(e){toast(e.message,true);$('load-osm').disabled=false;$('load-osm').textContent='Retry street map';}
}
function setSelection(kind){state.selecting=kind;$('map-selection').hidden=false;toast(`Select the ${kind==='origin'?'start':'finish'} location on the map.`);}
function pickLocation(coordinate) {
  const kind=state.selecting;state.selecting=null;$('map-selection').hidden=true;
  if(state.trip){event('preferences_changed',{[kind]:coordinate});}
  else{state[kind]=coordinate;renderMap();}
  $('endpoint-text').textContent=`${kind==='origin'?'Start':'Finish'}: ${coordinate.lat.toFixed(4)}, ${coordinate.lon.toFixed(4)}`;
}
function placeLabel(place) {return place.country?`${place.name}, ${place.country}`:place.name;}
function cityStatus(text,warning=false) {const n=$('city-status');n.textContent=text;n.classList.toggle('warning',warning);}
function unsupportedCity(city) {
  // Fixture servers list their cities in config.supported_cities; live servers send null (any geocodable city).
  const cities=state.config?.supported_cities;
  return !!(city&&Array.isArray(cities)&&cities.length&&!cities.some(c=>c.toLowerCase()===city.trim().toLowerCase()));
}
function offerNewTrip(city) {
  // An existing trip keeps its city; the way to plan another one is a single click next to where the user typed it.
  const n=$('city-status');n.append(document.createTextNode(` Planning ${city} starts a new trip.`));
  n.append(button(`New trip in ${city} ↗`,'text-button',()=>startNewTrip(city)));
}
function startNewTrip(city) {
  try{sessionStorage.setItem('travel-next-city',city);}catch{}
  $('new-trip').onclick();
}
function fixtureWarning(place) {
  // The fixture catalog covers central Berlin only; the server reports fixture_supported per geocoded place.
  $('city-status').classList.add('warning');
  toast(`Fixture data covers central Berlin only. Start the live server (scripts/serve_live.sh) to plan ${place.name}.`,false,true);
}
async function locateCity() {
  // GET /api/geocode?q= moves the start and finish to the city and sets its timezone.
  // Resolves true when the city can be planned in the current data mode, false otherwise.
  if(state.busy||state.locating)return false;
  const query=$('city').value.trim();
  if(!query){cityStatus('Enter a city name first.',true);return false;}
  state.locating=true;$('locate-button').disabled=true;cityStatus(`Locating ${query}...`);
  try {
    const place=await api(`/api/geocode?q=${encodeURIComponent(query)}`);
    const coordinate={lat:Number(place.lat),lon:Number(place.lon)};
    state.origin=coordinate;state.destination=coordinate;state.locatedCity=query;state.locatedPlace=place;
    if(place.timezone)$('timezone').value=place.timezone;
    const title=$('trip-title');
    if(title.value===state.config.default_request.title||title.value===state.autoTitle){state.autoTitle=`A day in ${place.name}`;title.value=state.autoTitle;}
    $('endpoint-text').textContent=`Start and finish: ${placeLabel(place)}`;
    cityStatus(`Located ${placeLabel(place)} · ${place.timezone} · ${coordinate.lat.toFixed(4)}, ${coordinate.lon.toFixed(4)}`);
    if(state.trip?.itinerary&&query.toLowerCase()!==state.trip.request.city.trim().toLowerCase())offerNewTrip(place.name);
    updateDetailsMeta();render();
    if(state.map)state.map.setView([coordinate.lat,coordinate.lon],13);
    if(state.config.data_mode==='fixture'&&place.fixture_supported===false){fixtureWarning(place);return false;}
    return true;
  }catch(e){cityStatus(e.message,true);toast(e.message,true);return false;}
  finally{state.locating=false;$('locate-button').disabled=state.busy;}
}
async function cityReady() {
  // Build itinerary geocodes a city that was typed but not located, and refuses one the fixture cannot serve.
  if(!state.config.geocoding)return true;
  if($('city').value.trim()!==state.locatedCity)return locateCity();
  if(state.config.data_mode==='fixture'&&state.locatedPlace?.fixture_supported===false){fixtureWarning(state.locatedPlace);return false;}
  return true;
}
async function submitTrip() {
  // Shared by the form's submit and the chat's Build chip: a new trip is planned, an existing one gets preferences_changed.
  try {
    if(state.proposal){toast('Apply or reject the pending revision first.');return;}
    if(state.busy||state.locating)return;
    if(state.trip?.itinerary) {
      const request=buildRequest();
      if(request.city.trim()!==state.trip.request.city.trim()) {
        toast('Choose New trip to plan another city.');cityStatus(`This trip stays in ${state.trip.request.city}.`,true);offerNewTrip(request.city.trim());return;
      }
      if(request.start!==new Date(state.trip.request.start).toISOString()||request.end!==new Date(state.trip.request.end).toISOString()) {
        toast('Choose New trip to change the date or available period.');cityStatus('This trip keeps its date and hours.',true);offerNewTrip(state.trip.request.city);return;
      }
      await event('preferences_changed',{interests:request.interests,budget_minor:request.budget_minor,max_walking_m:request.max_walking_m,
        transport_mode:request.transport_mode,target_stops:request.target_stops,avoid_rain_outdoor_visits:request.avoid_rain_outdoor_visits});
      return;
    }
    if(!(await cityReady()))return;
    // buildRequest runs after locateCity so the geocoded endpoints and timezone are part of the request.
    const request=buildRequest();
    state.flowerRun=null;state.jobId=null;busy(true);await watchJob(await post('/api/trips',{request}));
  }catch(e){busy(false);toast(e.message,true);}
}
function appendChatTurn(role,text,extra='') {
  const item=node('div',`chat-turn ${role}${extra?' '+extra:''}`,text);$('chat-log').append(item);$('chat-log').scrollTop=$('chat-log').scrollHeight;
  return item;
}
function chatStatus(data) {
  // Visibility of system status: what the intake still needs, whether this server can plan the city, and how it was read.
  const missing=(data.missing||[]).map(m=>m.replaceAll('_',' '));
  const engine=data.engine==='model'?'read with the model':'read offline, no model call';
  const city=data.brief?.city;
  $('chat-note').textContent=missing.length?`Still needed: ${missing.join(' · ')} · ${engine}.`:
    data.ready?`Brief complete · ${engine}.`:
    unsupportedCity(city)?`Brief complete, but ${city} is outside this server's data · ${engine}.`:`Brief complete, but not plannable yet (see the reply above) · ${engine}.`;
}
function applyBrief(data) {
  const brief=data.brief||{},previous=state.locatedCity;
  if(state.trip?.itinerary) {
    // An existing trip keeps its city, date and endpoints; the conversation can only adjust preferences.
    const req={...state.trip.request};
    if(brief.budget_minor!=null)req.budget_minor=brief.budget_minor;
    if(brief.max_walking_m!=null)req.max_walking_m=brief.max_walking_m;
    if(brief.transport_mode)req.transport_mode=brief.transport_mode;
    if(brief.target_stops!=null)req.target_stops=brief.target_stops;
    if(brief.avoid_rain_outdoor_visits!=null)req.avoid_rain_outdoor_visits=brief.avoid_rain_outdoor_visits;
    if(brief.interests?.length)req.interests=[...brief.interests];
    syncForm(req);
    const otherCity=brief.city&&brief.city.trim().toLowerCase()!==state.trip.request.city.trim().toLowerCase();
    const otherDate=brief.date&&brief.date!==dateAt(state.trip.request.start,state.trip.request.timezone);
    if(otherCity||otherDate)appendChatTurn('assistant','Changing the city or date starts a new trip: choose New trip in the top bar. Preferences apply to the current itinerary.','note');
    return;
  }
  if(data.ready&&data.request) {
    // The server built this request from the brief and its own geocoder: city, timezone and endpoints are authoritative.
    syncForm(data.request);state.locatedPlace=null;
    const o=data.request.origin;
    $('endpoint-text').textContent=`Start and finish: ${data.request.city} (${o.lat.toFixed(4)}, ${o.lon.toFixed(4)})`;
    cityStatus(`Located ${data.request.city} · ${data.request.timezone}`);
    return;
  }
  syncForm(briefToRequestFields(brief));
  // A partial brief has not been geocoded: keep the earlier located city, or force a lookup when it changed.
  const changed=brief.city&&brief.city.trim()!==previous;
  state.locatedCity=changed?null:previous;
  if(changed&&unsupportedCity(brief.city))cityStatus(`${brief.city} is outside this server's data (${state.config.supported_cities.join(', ')} only). Press Locate or Build itinerary to find ${brief.city} on the map anyway.`,true);
  else if(changed)cityStatus(`Press Locate or Build itinerary to find ${brief.city}.`);
}
function tryBuildFromChat() {
  if(state.busy){appendChatTurn('assistant','Planning is still running. These details stay in the form for the next build.','note');return;}
  if(state.proposal){appendChatTurn('assistant','A proposed revision is waiting in Changes & evidence. Apply it or keep the current itinerary first.','note');return;}
  submitTrip();
}
async function chatTurn(text) {
  text=text.trim();if(!text||state.chatBusy)return;
  appendChatTurn('user',text);state.chatHistory.push({role:'user',text});
  state.chatBusy=true;$('chat-send').disabled=true;$('chat-send').textContent='Sending...';$('chat-note').textContent='Reading your message...';
  try {
    const data=await post('/api/intake',{message:text,brief:state.brief,history:state.chatHistory.slice(-20)});
    state.brief=data.brief||{};
    if(data.reply){appendChatTurn('assistant',data.reply);state.chatHistory.push({role:'assistant',text:data.reply});}
    for(const note of data.notes||[])appendChatTurn('assistant',note,'note');
    applyBrief(data);chatStatus(data);
    if(!data.ready&&!(data.missing||[]).length&&unsupportedCity(data.brief?.city)&&!state.trip?.itinerary) {
      // "Want a Berlin day instead?" needs a one-click yes: the rules parser does not understand a bare "yes".
      const city=state.config.supported_cities[0];
      const chip=button(`Plan ${city} instead ↗`,'chat-quick-chip',()=>{chip.disabled=true;chatTurn(city);});
      $('chat-log').append(chip);$('chat-log').scrollTop=$('chat-log').scrollHeight;
    }
    if(data.ready&&data.request) {
      $('details-panel').open=false;
      if(!state.trip?.itinerary&&!state.busy&&!state.proposal) {
        // The reply promises the build ("Building your day in X now"), so the first plan starts without another click.
        appendChatTurn('assistant','Starting the build. You can still adjust the trip details below and choose Update preferences afterwards.','note');
        await submitTrip();
      } else {
        // Changing an existing itinerary is an explicit step: it sends preferences_changed and yields a reviewable revision.
        const chip=button('Update itinerary ↗','chat-build-chip',()=>{chip.disabled=true;tryBuildFromChat();});
        $('chat-log').append(chip);$('chat-log').scrollTop=$('chat-log').scrollHeight;
      }
    }
  }catch(e){
    $('chat-note').textContent='';toast(e.message,true);
    appendChatTurn('assistant','I could not process that message. Try again, or fill in the trip details directly below.','note');
  }
  finally{state.chatBusy=false;$('chat-send').disabled=false;$('chat-send').textContent='Send';$('chat-input').focus();}
}
async function boot() {
  state.config=await api('/api/config');
  const geocoding=!!state.config.geocoding;$('locate-button').hidden=!geocoding;$('city-status').hidden=!geocoding;
  const intake=!!state.config.intake;$('chat-intake').hidden=!intake;if(!intake)$('details-panel').open=true;
  try{const notice=sessionStorage.getItem('travel-notice');if(notice){sessionStorage.removeItem('travel-notice');toast(notice);}}catch{}
  const live=state.config.data_mode==='live',router=(state.config.router||'router').toUpperCase();
  $('mode-pill').textContent=state.config.execution_backend==='flower'?`Flower SuperGrid · ${state.config.model||'runtime model'}`:
    state.config.agent_mode==='model'?`${live?'Live':state.config.data_mode} data · ${state.config.model||'model'}`:
    live?`Live data · ${router}`:'Fixture replay · 0 model calls';
  $('notice-bar').textContent=live?state.config.live_notice||
    'Live provider data. Missing prices and hours remain unknown. Review all proposed changes. No bookings or payments.':state.config.fixture_notice;
  $('engine-note').textContent=state.config.execution_backend==='flower'?`Four role-specific model contexts run as a Flower AgentApp on SuperGrid with ${state.config.model||'the runtime model'}. Data: ${state.config.data_mode}.`:
    state.config.agent_mode==='rules'?'Rule-based specialist replay. No model API calls are made.':
    'Four role-specific model contexts. The configured model runs through the selected execution backend.';
  const cities=state.config.supported_cities;
  $('chat-note').textContent=cities?`This server plans ${cities.join(', ')} from bundled fixture data. Other cities need the live server.`:'Any city works: I look it up for you.';
  $('chat-input').placeholder=cities?`e.g. ${cities[0]} next Friday, 10 to 5, art and coffee`:'e.g. Lisbon on Saturday from 9 to 6, food and history';
  syncForm(state.config.default_request);initAgents();render();
  if(state.config.auth_required) {
    try{await api('/api/trips');}catch{return;}
  }
  const saved=localStorage.getItem('travel-trip');
  if(saved){try{state.tripId=saved;const data=await api(`/api/trips/${saved}`);state.trip=data.trip;
    state.proposal=data.proposals.find(p=>p.base_revision===data.trip.revision)||null;syncForm(data.trip.request);render();
    for(const r of (view()?.reports||[]))addEvent({type:'agent.completed',data:{role:r.role,report:r}});
    if(state.proposal)toast('A proposed revision is still waiting for your review.');
  }catch{localStorage.removeItem('travel-trip');}}
  // "New trip in <city>" from the previous page: prefill and locate the city so planning it takes one step less.
  let next=null;try{next=sessionStorage.getItem('travel-next-city');sessionStorage.removeItem('travel-next-city');}catch{}
  if(next&&!state.trip) {
    $('city').value=next;state.locatedCity=null;updateDetailsMeta();
    if(!$('chat-intake').hidden)appendChatTurn('assistant',`New trip in ${next}. Tell me the date, hours and interests, or fill in the trip details below.`,'note');
    await locateCity();
  }
}
$('trip-form').onsubmit=e=>{e.preventDefault();submitTrip();};
$('budget').oninput=updateRangeLabels;$('walking').oninput=updateRangeLabels;
$('locate-button').onclick=()=>locateCity();
$('city').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();locateCity();}};
$('city').oninput=()=>{const c=$('city').value.trim();updateDetailsMeta();if(state.config?.geocoding&&c&&c!==state.locatedCity)cityStatus(`Press Locate or Build itinerary to find ${c}.`);};
for(const id of ['trip-date','start-time','end-time'])$(id).oninput=updateDetailsMeta;
$('new-trip').onclick=()=>{
  if(state.busy) {
    // Reloading does not cancel the job: the server keeps executing it and SuperGrid keeps charging for it.
    const subject=state.flowerRun?.run_id?`SuperGrid run ${state.flowerRun.run_id}`:`planning job${state.jobId?' '+state.jobId:''}`;
    const notice=state.config?.execution_backend==='flower'
      ?`The previous ${subject} continues in the background and will still be charged. Its result stays with the earlier trip.`
      :`The previous ${subject} continues in the background. Its result stays with the earlier trip.`;
    try{sessionStorage.setItem('travel-notice',notice);}catch{}
  }
  localStorage.removeItem('travel-trip');location.reload();
};
$('rain').onclick=()=>event('rain',{},true);
$('less-walking').onclick=()=>event('pace_changed',{max_walking_m:Math.max(300,Math.round((state.trip?.itinerary.walking_m||2000)*.6))});
$('refresh').onclick=()=>event('weather_updated');$('load-osm').onclick=loadOSM;
$('set-start').onclick=()=>setSelection('origin');$('set-end').onclick=()=>setSelection('destination');
$('cancel-selection').onclick=()=>{state.selecting=null;$('map-selection').hidden=true;};
$('close-details').onclick=()=>$('details-dialog').close();
$('export-button').onclick=()=>{if(state.trip)location.href=`/api/trips/${state.trip.id}/export`;};
$('import-button').onclick=()=>$('import-file').click();
$('import-file').onchange=async e=>{try{const file=e.target.files[0];if(!file)return;if(file.size>1000000)throw new Error('Import is limited to 1 MB.');
  const result=await post('/api/trips/import',JSON.parse(await file.text()));state.tripId=result.trip_id;localStorage.setItem('travel-trip',state.tripId);await reloadTrip();toast('Imported itinerary validated.');
}catch(e){toast(e.message,true);}};
$('copy-command').onclick=async()=>{try{await navigator.clipboard.writeText(state.command);toast('Private job command copied.');}catch{toast('Select and copy the command manually.');}};
$('login-form').onsubmit=async e=>{e.preventDefault();try{await post('/api/login',{token:$('login-token').value});$('login-token').value='';$('login-dialog').close();await boot();}catch(e){$('login-error').textContent=e.message;}};
$('chat-form').onsubmit=e=>{e.preventDefault();const text=$('chat-input').value;$('chat-input').value='';chatTurn(text);};
boot().catch(e=>toast(e.message,true));
