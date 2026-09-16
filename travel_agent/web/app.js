'use strict';
const $ = (id) => document.getElementById(id);
const state = {config:null, trip:null, proposal:null, busy:false, events:[], selectedInterests:new Set(),
  origin:null, destination:null, map:null, layer:null, selecting:null, stream:null, command:null,
  brief:{}, chatHistory:[], chatBusy:false, selectedDay:0};
const ROLES = {
  discovery: ['Discovery', 'Places and personal interests', '⌕'],
  conditions: ['Conditions', 'Weather and activity exposure', '☂'],
  mobility: ['Mobility', 'Routes and travel intervals', '↗'],
  budget_pace: ['Budget & pace', 'Cost, walking and breaks', '≋']
};
const TYPES = ['art','architecture','parks','coffee','history','food','books','shopping'];
function node(tag, className, text) {
  const n=document.createElement(tag); if(className)n.className=className;
  if(text!==undefined)n.textContent=text; return n;
}
function button(text, className, fn) {const b=node('button',className,text);b.type='button';b.onclick=fn;return b;}
function toast(message,error=false) {
  const n=$('toast');n.textContent=message;n.classList.toggle('error',error);n.hidden=false;
  clearTimeout(toast.timer);toast.timer=setTimeout(()=>n.hidden=true,7000);
}
async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
  const data=await response.json();
  if(!response.ok) {
    if(response.status===401 && !$('login-dialog').open)$('login-dialog').showModal();
    const message=typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data);
    throw new Error(message);
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
  interestButtons();updateRangeLabels();
}
function updateRangeLabels() {
  $('budget-label').textContent=`${state.trip?.request.currency||'EUR'} ${$('budget').value}`;
  $('walking-label').textContent=`${Number($('walking').value).toFixed(1)} km`;
}
function buildRequest() {
  const base=structuredClone(state.config.default_request);
  return {...base,title:$('trip-title').value,city:$('city').value,timezone:$('timezone').value,
    start:zonedISO($('trip-date').value,$('start-time').value,$('timezone').value),
    end:zonedISO($('trip-date').value,$('end-time').value,$('timezone').value),
    days:(!state.trip&&state.brief?.days)?state.brief.days:base.days,
    origin:state.origin,destination:state.destination,interests:[...state.selectedInterests],
    budget_minor:Math.round(Number($('budget').value)*100),max_walking_m:Math.round(Number($('walking').value)*1000),
    transport_mode:$('transport').value,target_stops:+$('stops').value,avoid_rain_outdoor_visits:$('avoid-rain').checked};
}
function briefToRequestFields(brief) {
  const merged=buildRequest();
  const tz=brief.timezone||merged.timezone;
  if(brief.city)merged.city=brief.city;
  if(brief.title)merged.title=brief.title;
  if(brief.timezone)merged.timezone=brief.timezone;
  if(brief.date&&brief.start_time)merged.start=zonedISO(brief.date,brief.start_time,tz);
  if(brief.date&&brief.end_time)merged.end=zonedISO(brief.date,brief.end_time,tz);
  if(brief.budget_minor!=null)merged.budget_minor=brief.budget_minor;
  if(brief.max_walking_m!=null)merged.max_walking_m=brief.max_walking_m;
  if(brief.transport_mode)merged.transport_mode=brief.transport_mode;
  if(brief.target_stops!=null)merged.target_stops=brief.target_stops;
  if(brief.avoid_rain_outdoor_visits!=null)merged.avoid_rain_outdoor_visits=brief.avoid_rain_outdoor_visits;
  if(brief.interests&&brief.interests.length)merged.interests=[...brief.interests];
  if(brief.days!=null)merged.days=brief.days;
  return merged;
}
function busy(value,label='Planning') {
  state.busy=value;$('build-button').disabled=value;
  $('job-state').textContent=value?label:'Ready';$('job-state').classList.toggle('busy',value);
  for(const id of ['rain','less-walking','refresh'])$(id).disabled=value||!state.trip?.itinerary||!!state.proposal;
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
    const item=node('div','feed-event');item.append(node('strong','',ROLES[d.role][0]),node('p','',d.report.summary));$('event-feed').append(item);
  }
  if(event.type==='collaboration.message' && (d.kind==='request' || d.recipient==='user')) {
    const item=node('div','feed-event handoff');item.append(node('strong','',`${d.sender.toUpperCase()} > ${d.recipient.toUpperCase()}`),node('p','',d.summary));$('event-feed').append(item);
  }
  if(event.type==='agent.completed'&&d.report) {
    const meta=`${d.report.candidate_ids.length} ranked · ${d.report.avoid_ids.length} to avoid · ${d.report.evidence_ids.length} evidence refs`;
    $('event-feed').lastElementChild?.append(node('small','feed-meta',meta));
  }
  if(event.type==='model.completed') {
    const item=node('div','feed-event model');item.append(node('strong','',`${ROLES[d.role]?.[0]||d.role} · model call ${d.call_number}`),node('p','',`${d.returned_model||d.requested_model} answered in ${d.elapsed_s}s`));$('event-feed').append(item);
  }
  if(event.type==='model.repair') {
    const item=node('div','feed-event');item.append(node('strong','',`${ROLES[d.role]?.[0]||d.role} · repair turn`),node('p','',d.reason));$('event-feed').append(item);
  }
  if(event.type==='tool.completed'||event.type==='tool.failed') {
    const item=node('div','feed-event tool');item.append(node('strong','',`${ROLES[d.role]?.[0]||d.role} · tool ${d.name}`),node('p','',event.type==='tool.failed'?`Rejected: ${d.error}`:'Read bounded evidence for this trip.'));$('event-feed').append(item);
  }
  if(event.type==='evidence.loaded') {
    const item=node('div','feed-event');item.append(node('strong','','Evidence loaded'),node('p','',`${d.place_count} candidate places · weather ${d.weather_status} · ${d.data_mode} data`));$('event-feed').append(item);
  }
  if(event.type==='validation.completed') {
    const item=node('div','feed-event');item.append(node('strong','','Deterministic validation'),node('p','',`${d.status} · ${d.issues.length} findings`));$('event-feed').append(item);
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
    if(d.metrics)$('flower-run').textContent=`Run ${d.run_id} · ${d.metrics.model_calls} model calls · ${d.metrics.elapsed_s}s on SuperGrid`;
  }
  if(event.type==='proposal.ready'||event.type==='job.finished') {
    if(d.metrics)$('run-metrics').textContent=`${d.metrics.model_calls} model calls · ${d.metrics.tool_calls} tool calls · ${d.metrics.elapsed_s}s`;
  }
  if(event.type==='job.failed'){toast(d.error,true);if(state.config.execution_backend==='flower')$('flower-title').textContent='SuperGrid run failed';}
  $('event-feed').scrollTop=$('event-feed').scrollHeight;
}
async function watchJob(result) {
  const job=result.job;state.tripId=result.trip_id;localStorage.setItem('travel-trip',state.tripId);
  initAgents();state.events=[];$('event-feed').replaceChildren();$('run-metrics').textContent='Collecting execution measurements...';
  busy(true,job.external?'Awaiting Flower':state.config.execution_backend==='flower'?'SuperGrid':'Planning');
  $('flower-command').hidden=true;$('copy-command').hidden=true;$('flower-run').hidden=true;
  if(job.flower_command) {
    state.command=job.flower_command;$('flower-command').textContent=job.flower_command;$('flower-panel').hidden=false;
    $('flower-title').textContent='Run this planning job';$('flower-note').textContent='Copy this private command into your authenticated project terminal. Its credential is scoped to this job.';
    $('flower-command').hidden=false;$('copy-command').hidden=false;
    toast('Start the scoped Flower command shown in the right panel.');
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
    }catch(e){toast(e.message,true);}finally{busy(false);}
  }
  source.addEventListener('done',finish);
  source.onerror=async()=>{
    try{const j=await api(`/api/jobs/${job.id}`);if(!['running','queued'].includes(j.status))await finish();}catch(e){source.close();busy(false);toast(e.message,true);}
  };
}
async function event(kind,payload={},simulated=false) {
  if(!state.trip||state.busy)return;
  if(state.proposal){toast('Apply or reject the pending revision first.');return;}
  try {
    busy(true);
    const body={base_revision:state.trip.revision,event:{id:crypto.randomUUID().replaceAll('-',''),kind,payload,simulated}};
    await watchJob(await post(`/api/trips/${state.trip.id}/events`,body));
  }catch(e){busy(false);toast(e.message,true);}
}
async function reloadTrip() {
  const data=await api(`/api/trips/${state.tripId}`);state.trip=data.trip;state.proposal=null;syncForm(state.trip.request);render();busy(false);
}
function render() {
  const trip=view(), plan=trip?.itinerary;
  $('itinerary-title').textContent=trip?.request.title||'Explore Berlin, together.';
  $('revision').textContent=state.proposal?`PROPOSED R${trip.revision}`:trip?`REVISION ${trip.revision}`:'DRAFT';
  const totalStops=(plan?.days||[]).reduce((n,d)=>n+d.stops.length,0);
  $('metric-stops').textContent=totalStops||0;
  $('metric-walk').replaceChildren(document.createTextNode(((plan?.walking_m||0)/1000).toFixed(1)+' '),node('small','','km'));
  $('metric-cost').replaceChildren(document.createTextNode(((plan?.cost_minor||0)/100).toFixed(2)+' '),node('small','',trip?.request.currency||'EUR'));
  const lastDay=plan?.days?.at(-1);
  $('metric-end').textContent=time(lastDay?.end_arrival||trip?.request.end||state.config?.default_request.end);
  $('export-button').disabled=!state.trip?.itinerary;
  if(plan?.days&&state.selectedDay>=plan.days.length)state.selectedDay=0;
  renderDayTabs();renderTimeline();renderReview();renderMap();busy(state.busy);
}
function renderDayTabs() {
  const trip=view(),plan=trip?.itinerary,days=plan?.days||[];
  const tabs=$('day-tabs');tabs.replaceChildren();
  if(days.length<2){tabs.hidden=true;return;}
  tabs.hidden=false;
  days.forEach((day,i)=>{
    const b=button(`Day ${i+1}`,'day-tab',()=>{state.selectedDay=i;renderDayTabs();renderMap();});
    b.classList.toggle('active',i===state.selectedDay);tabs.append(b);
  });
}
function renderTimeline() {
  const trip=view(),plan=trip?.itinerary;if(!plan)return;
  $('timeline').replaceChildren();
  const days=plan.days||[];
  if(!days.some(d=>d.stops.length)){$('timeline').append(node('div','empty-timeline','No feasible schedule was found under the current constraints.'));return;}
  days.forEach(day=>{
    const section=node('section','day-section');
    const heading=node('div','day-section-heading');
    heading.append(node('strong','',`Day ${day.index+1} · ${day.date}`),
      node('span','',`${day.stops.length} stop${day.stops.length===1?'':'s'} · ${(day.walking_m/1000).toFixed(1)} km · ${formatMoney(day.cost_minor,trip.request.currency)}`));
    section.append(heading);
    const completedBefore=day.stops.filter(s=>s.completed).length;
    day.stops.forEach((stop,index)=>{
      const leg=day.legs[index];
      if(leg)section.append(node('div','transfer-row',`${leg.mode==='walking'?'Walk':'Cycle'} ${Math.ceil(leg.duration_s/60)} min · ${leg.distance_m} m${leg.source_status==='fixture'?' · synthetic route':''}`));
      const card=node('article','timeline-card');
      card.append(node('div','stop-index'+(stop.completed?' completed':''),stop.completed?'✓':String(index+1)));
      const content=node('div');content.append(node('div','stop-time',`${time(stop.start)} to ${time(stop.end)}`));
      const title=node('div','stop-title',stop.name);title.onclick=()=>showPlace(stop.place_id);title.tabIndex=0;title.onkeydown=e=>{if(e.key==='Enter')showPlace(stop.place_id);};
      if(stop.locked)title.append(node('span','stop-tag locked','LOCKED'));
      if(stop.completed)title.append(node('span','stop-tag','COMPLETED'));
      const price=stop.cost_minor===null?'Price unknown':formatMoney(stop.cost_minor,trip.request.currency);
      const place=trip.places.find(p=>p.id===stop.place_id);
      content.append(title,node('div','stop-meta',`${place?.indoor===true?'Indoor':place?.indoor===false?'Outdoor':'Exposure unknown'} · ${Math.round((new Date(stop.end)-new Date(stop.start))/60000)} min · ${price}`));
      card.append(content);const actions=node('div','stop-actions');
      if(!stop.completed&&!state.proposal&&day.index===0) {
        actions.append(button(stop.locked?'Unlock':'Lock time','stop-action',()=>event(stop.locked?'unlock_stop':'lock_stop',{place_id:stop.place_id})));
        actions.append(button('Replace','stop-action',()=>event('preferences_changed',{excluded_place_ids:[...state.trip.request.excluded_place_ids,stop.place_id]})));
        if(index===state.trip.progress.completed_place_ids.length)actions.append(button('Complete','stop-action',()=>completeStop(stop)));
      }
      card.append(actions);section.append(card);
      const pause=day.breaks.find(b=>b.location_id===stop.place_id&&new Date(b.start)>=new Date(stop.end));
      if(pause)section.append(node('div','transfer-row',`Break ${time(pause.start)} to ${time(pause.end)} · ${pause.reason}`));
    });
    const last=day.legs.at(-1);if(last)section.append(node('div','transfer-row',`${Math.ceil(last.duration_s/60)} min ${last.mode} to finish · arrival ${time(day.end_arrival)}`));
    $('timeline').append(section);
  });
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
  summary.append(node('div','',`${plan.validation.status[0].toUpperCase()+plan.validation.status.slice(1)} · ${plan.unknown_cost_count} unknown prices`));root.append(summary);
  if(state.proposal) {
    const names=new Map([...(state.trip?.places||[]),...trip.places].map(p=>[p.id,p.name]));
    const trigger=state.proposal.trigger.replaceAll('_',' ');
    const simulated=trip.weather_override?' (simulated scenario)':'';
    root.append(node('p','micro',`Trigger: ${trigger}${simulated}. Coordinator proposal built from ${trip.reports.length} specialist reports.`));
    for(const m of trip.messages.filter(m=>m.kind==='request')) {
      const row=node('div','change-row');row.append(node('span','change-mark',ROLES[m.sender]?.[2]||'·'),node('span','',`${ROLES[m.sender]?.[0]||m.sender} asked ${ROLES[m.recipient]?.[0]||m.recipient}: ${m.summary}`));root.append(row);
    }
    for(const [ids,mark,cls]of [[state.proposal.removed,'−','removed'],[state.proposal.added,'+',''],[state.proposal.preserved,'=','']]) {
      ids.forEach(id=>{const row=node('div','change-row');row.append(node('span','change-mark '+cls,mark),node('span','',names.get(id)||id));root.append(row);});
    }
    const locks=trip.request.reservations.length;if(locks)root.append(node('p','micro',`${locks} locked reservation(s) protected.`));
    const actions=node('div','review-actions');
    const accept=button('Apply revision','primary',async()=>{try{await post(`/api/proposals/${state.proposal.id}/accept`);await reloadTrip();toast('Revision applied.');}catch(e){toast(e.message,true);}});
    accept.disabled=!plan.validation.valid;
    actions.append(accept,button('Keep current','text-button',async()=>{try{await post(`/api/proposals/${state.proposal.id}/reject`);await reloadTrip();}catch(e){toast(e.message,true);}}));root.append(actions);
  } else root.append(node('p','micro','Change the weather, walking limit, or a stop to inspect a coordinated revision.'));
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
function selectedDayPlan() {
  const trip=view();return trip?.itinerary?.days?.[state.selectedDay]||null;
}
function bounds() {
  const trip=view();const points=[state.origin||state.config.default_request.origin,state.destination||state.config.default_request.destination,...(trip?.places||[]).map(p=>p.coordinate)];
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
  const oldDay=state.trip?.itinerary?.days?.[state.selectedDay];
  if(state.proposal)route(oldDay?.legs,true);route(selectedDayPlan()?.legs);
  const active=new Map((selectedDayPlan()?.stops||[]).map((s,i)=>[s.place_id,i+1]));
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
  $('route-label').textContent=trip?.data_mode==='fixture'?'Synthetic direct-line routes. Not walking directions.':'Coordinate schematic. Load OSM for the road map.';
  s.onclick=e=>{
    if(!state.selecting)return;
    const rect=s.getBoundingClientRect(),x=(e.clientX-rect.left)/rect.width*900,y=(e.clientY-rect.top)/rect.height*590;
    pickLocation({lon:b.minLon+(x-40)/820*(b.maxLon-b.minLon),lat:b.maxLat-(y-70)/450*(b.maxLat-b.minLat)});
  };
}
function renderLeaflet() {
  const L=window.L,trip=view();state.layer.clearLayers();let box=[];
  for(const l of selectedDayPlan()?.legs||[]) {
    if(l.geometry.length>1)L.geoJSON({type:'Feature',properties:{},geometry:{type:'LineString',coordinates:l.geometry}},
      {style:{color:'#ba5a34',weight:4,dashArray:l.source_status==='fixture'?'8 7':null}}).addTo(state.layer);
  }
  const active=new Map((selectedDayPlan()?.stops||[]).map((s,i)=>[s.place_id,i+1]));
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
    $('map-label').textContent='OPENSTREETMAP';$('load-osm').textContent='OSM loaded';renderLeaflet();
  }catch(e){toast(e.message,true);$('load-osm').disabled=false;$('load-osm').textContent='Retry OSM map';}
}
function setSelection(kind){state.selecting=kind;$('map-selection').hidden=false;toast(`Select the ${kind==='origin'?'start':'finish'} location on the map.`);}
function pickLocation(coordinate) {
  const kind=state.selecting;state.selecting=null;$('map-selection').hidden=true;
  if(state.trip){event('preferences_changed',{[kind]:coordinate});}
  else{state[kind]=coordinate;renderMap();}
  $('endpoint-text').textContent=`${kind==='origin'?'Start':'Finish'}: ${coordinate.lat.toFixed(4)}, ${coordinate.lon.toFixed(4)}`;
}
function appendChatTurn(role,text) {
  const item=node('div','chat-turn '+role,text);$('chat-log').append(item);$('chat-log').scrollTop=$('chat-log').scrollHeight;
  return item;
}
function tryBuildFromChat() {
  if(state.trip?.itinerary) {
    const req=briefToRequestFields(state.brief);
    const changedCity=req.city!==state.trip.request.city;
    const changedPeriod=req.start!==new Date(state.trip.request.start).toISOString()||req.end!==new Date(state.trip.request.end).toISOString();
    if(changedCity||changedPeriod) {
      appendChatTurn('assistant','That would change the date, time window, or city of an existing trip — choose New trip to start a fresh one.');
      return;
    }
  }
  $('trip-form').requestSubmit();
}
async function chatTurn(text) {
  if(!text.trim()||state.chatBusy)return;
  appendChatTurn('user',text);state.chatHistory.push({role:'user',text});
  $('chat-note').textContent='';state.chatBusy=true;$('chat-send').disabled=true;
  try {
    const data=await post('/api/intake',{message:text,brief:state.brief,history:state.chatHistory.slice(-20)});
    state.brief=data.brief;
    if(data.reply) {
      appendChatTurn('assistant',data.reply);state.chatHistory.push({role:'assistant',text:data.reply});
    }
    syncForm(briefToRequestFields(state.brief));
    for(const note of data.notes||[])$('chat-note').textContent=note;
    if(data.ready&&data.request) {
      const chip=node('button','chat-build-chip','Build itinerary ↗');chip.type='button';
      chip.onclick=()=>{chip.disabled=true;tryBuildFromChat();};
      $('chat-log').append(chip);$('chat-log').scrollTop=$('chat-log').scrollHeight;
      $('details-panel').open=false;
    }
  }catch(e){toast(e.message,true);}
  finally{state.chatBusy=false;$('chat-send').disabled=false;}
}
async function boot() {
  state.config=await api('/api/config');
  $('mode-pill').textContent=state.config.execution_backend==='flower'?`Flower SuperGrid · ${state.config.model||'runtime model'}`:
    state.config.agent_mode==='model'?`${state.config.data_mode} data · ${state.config.model||'model'}`:
    state.config.data_mode==='fixture'?'Fixture replay · 0 model calls':'Live data · rule-based policies';
  $('notice-bar').textContent=state.config.data_mode==='fixture'?state.config.fixture_notice:
    'Live provider data. Missing prices and hours remain unknown. Review all proposed changes. No bookings or payments.';
  $('engine-note').textContent=state.config.execution_backend==='flower'?`Four role-specific model contexts run as a Flower AgentApp on SuperGrid with ${state.config.model||'the runtime model'}. Data: ${state.config.data_mode}.`:
    state.config.agent_mode==='rules'?'Rule-based specialist replay. No model API calls are made.':
    'Four role-specific model contexts. The configured model runs through the selected execution backend.';
  syncForm(state.config.default_request);initAgents();render();
  if(state.config.auth_required) {
    try{await api('/api/trips');}catch{return;}
  }
  const saved=localStorage.getItem('travel-trip');
  if(saved){try{state.tripId=saved;const data=await api(`/api/trips/${saved}`);state.trip=data.trip;
    state.proposal=data.proposals.find(p=>p.base_revision===data.trip.revision)||null;syncForm(data.trip.request);render();
    for(const r of (view()?.reports||[]))addEvent({type:'agent.completed',data:{role:r.role,report:r}});
  }catch{localStorage.removeItem('travel-trip');}}
}
$('trip-form').onsubmit=async e=>{e.preventDefault();try {
  if(state.proposal){toast('Apply or reject the pending revision first.');return;}
  const request=buildRequest();
  if(state.trip?.itinerary) {
    if(request.start!==new Date(state.trip.request.start).toISOString()||request.end!==new Date(state.trip.request.end).toISOString()||request.city!==state.trip.request.city) {
      toast('Choose New trip to change the date, available period, or city.');return;
    }
    await event('preferences_changed',{interests:request.interests,budget_minor:request.budget_minor,max_walking_m:request.max_walking_m,
      transport_mode:request.transport_mode,target_stops:request.target_stops,avoid_rain_outdoor_visits:request.avoid_rain_outdoor_visits});
  } else {busy(true);await watchJob(await post('/api/trips',{request}));}
}catch(e){busy(false);toast(e.message,true);}};
$('budget').oninput=updateRangeLabels;$('walking').oninput=updateRangeLabels;
$('new-trip').onclick=()=>{if(state.busy){toast('Wait for the current job to finish.');return;}localStorage.removeItem('travel-trip');location.reload();};
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
