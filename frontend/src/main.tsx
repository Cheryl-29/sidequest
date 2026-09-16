import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowRight, Check, ChevronDown, Code2, ExternalLink, Footprints, LoaderCircle, LocateFixed, LockKeyhole, Plus, RefreshCw, SlidersHorizontal, TrainFront, Trash2, UnlockKeyhole, X } from 'lucide-react';
import type { Itinerary, Origin, PlanRequest, Proposal, Reason, RerollResponse, Result, Run, Session, Taste } from './types';
import './styles.css';

async function api<T>(path: string, body?: unknown, method = 'POST'): Promise<T> {
  const response = await fetch(`/api${path}`, {method: body === undefined ? 'GET' : method, headers: {'Content-Type': 'application/json'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.map((v: {msg: string}) => v.msg).join('；') || '请求失败，请重试');
  return data;
}
const time = (value: string) => new Intl.DateTimeFormat('en-AU', {timeZone: 'Australia/Sydney', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(value));
const duration = (minutes: number) => `${Math.floor(minutes / 60) ? `${Math.floor(minutes / 60)} 小时` : ''}${minutes % 60 ? ` ${minutes % 60} 分钟` : ''}`.trim();
function zoned(date: string, clock: string) {
  const naive = new Date(`${date}T${clock}:00Z`);
  const rendered = new Intl.DateTimeFormat('en-US', {timeZone:'Australia/Sydney', timeZoneName:'longOffset'}).format(naive);
  const offset = rendered.match(/GMT([+-]\d{2}:\d{2})/)?.[1] || '+10:00';
  return `${date}T${clock}:00${offset}`;
}
const clock = (minutes: number) => `${String(Math.floor(minutes / 60)).padStart(2,'0')}:${String(minutes % 60).padStart(2,'0')}`;
const toMinutes = (value: string) => Number(value.slice(0,2)) * 60 + Number(value.slice(3,5));
const names: Record<string,string> = {townhall:'Town Hall', central:'Central Station', circular:'Circular Quay', current:'当前位置'};

type When = 'now' | 'tonight' | 'tomorrow' | 'custom';
const durations: [number, string][] = [[30,'30 分钟'],[60,'1 小时'],[120,'2 小时'],[240,'半天'],[480,'一天']];
const examples = ['想找个安静的地方待一会儿', '出去散散步，吹吹风', '去看点艺术或历史', '想去海港边坐坐'];

// Display-only guess at what the free time was carved out of (plan v2 §2.4).
// It never reaches the request and the user can dismiss it.
function carvedFrom(date: string, start: number, minutes: number) {
  const day = new Date(`${date}T12:00:00Z`).getUTCDay();
  if (day === 0 || day === 6) return '周末';
  if (start >= 11 * 60 && start <= 14 * 60 && minutes <= 90) return '午休';
  if (start >= 17 * 60) return '下班后';
  return null;
}
// Reroll chips grouped by what they touch (plan v2.3 §2.2). D1/D2 feedback moves a session
// dimension; popularity feedback is a one-shot, same-kind reroll and is never remembered.
const tasteChips: [Reason,string][] = [['want_sit','想坐着'],['want_move','想动一动'],['too_far','太远了'],['want_farther','想走远点'],['too_obvious','太大众了'],['too_obscure','太冷门了']];
const kindOf = (tags: string[]) => tags.find(t => t.startsWith('kind:'))?.slice(5);
const sourceLabel = {user_stated:'你说的', agent_proposed:'你确认过的提议'};
const sharesStop = (a: Itinerary, b: Itinerary) => a.stops.some(s => b.stops.some(t => t.candidate.id === s.candidate.id));

function App() {
  const [session,setSession] = useState<Session>();
  const [date,setDate] = useState('');
  const [depart,setDepart] = useState('10:00');
  const [when,setWhen] = useState<When>('now');
  const [minutes,setMinutes] = useState(120);
  const [origin,setOrigin] = useState<Origin>('townhall');
  const [position,setPosition] = useState<{lat:number;lon:number}|null>(null);
  const [locating,setLocating] = useState(false);
  const [preference,setPreference] = useState('');
  const [walk,setWalk] = useState(90);
  const [budget,setBudget] = useState('');
  const [includeTransportCost,setIncludeTransportCost] = useState(false);
  const [mode,setMode] = useState<PlanRequest['mode']>('replay');
  const [adjustOpen,setAdjustOpen] = useState(false);
  const [run,setRun] = useState<Run>();
  const [result,setResult] = useState<Result>();
  const [selected,setSelected] = useState(0);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const [traceOpen,setTraceOpen] = useState(false);
  const [memoryOpen,setMemoryOpen] = useState(false);
  const [memoryText,setMemoryText] = useState('');
  const [taste,setTaste] = useState<Taste>();
  const [proposal,setProposal] = useState<Proposal|null>(null);
  const [accepted,setAccepted] = useState<string[]>([]);
  const [episodesOpen,setEpisodesOpen] = useState(false);
  const [savePreference,setSavePreference] = useState(false);
  const [rerollOpen,setRerollOpen] = useState(false);
  const [detailOpen,setDetailOpen] = useState(false);
  const [evidenceOpen,setEvidenceOpen] = useState(false);
  const [inferenceDismissed,setInferenceDismissed] = useState(false);
  const [notice,setNotice] = useState('');
  const [revisionText,setRevisionText] = useState('');
  const [strategy,setStrategy] = useState<'agent'|'fixed'>('fixed');
  const [progress,setProgress] = useState('');
  const [otherText,setOtherText] = useState('');
  const source = useRef<EventSource | null>(null);
  const generation = useRef(0);
  const questRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const plan = result?.itineraries[selected];
  // The server applies place and kind feedback to the stop the agent bet on, which the planner
  // may have put second; label the chips with that stop, not whatever is listed first.
  const anchor = plan && (plan.stops.find(s => s.candidate.id === result?.agent?.anchor_id) ?? plan.stops[0]).candidate;
  const endMinutes = toMinutes(depart) + minutes;
  const endTime = clock(endMinutes);
  const fitsToday = endMinutes < 24 * 60;

  useEffect(() => {
    api<Session>('/session').then(async s => {
      setSession(s); pickWhen('now', s);
      if (s.available_modes.includes('live')) setMode('live');
      if (s.agent_available) setStrategy('agent');
      await refreshTaste();
    }).catch(e => setError(e.message));
    return () => { source.current?.close(); generation.current++; };
  }, []);
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') {setTraceOpen(false);setMemoryOpen(false);} };
    document.addEventListener('keydown', key); return () => document.removeEventListener('keydown',key);
  }, []);

  // Bring the quest into view once the new run's area has rendered.
  useEffect(() => { if (busy && !result) questRef.current?.scrollIntoView({behavior:'smooth',block:'start'}); }, [busy]);
  const scrollToQuest = () => requestAnimationFrame(() => questRef.current?.scrollIntoView({behavior:'smooth',block:'start'}));

  async function refreshTaste() {
    try {setTaste(await api<Taste>('/taste'));} catch(e) {setError((e as Error).message);}
  }

  function pickWhen(next: When, s = session) {
    if (!s) return;
    setWhen(next);
    if (next === 'now') {
      const now = toMinutes(s.now.slice(11,16));
      setDate(s.today); setDepart(clock(Math.min(23 * 60 + 25, Math.ceil(now / 5) * 5)));
    } else if (next === 'tonight') {
      setDate(s.today); setDepart(clock(Math.max(18 * 60, Math.ceil(toMinutes(s.now.slice(11,16)) / 5) * 5)));
    } else if (next === 'tomorrow') {
      setDate(s.tomorrow); setDepart('10:00');
    }
  }

  function useCurrentLocation() {
    if (!navigator.geolocation) {setError('当前浏览器不支持位置服务。');return;}
    setLocating(true);setError('');
    navigator.geolocation.getCurrentPosition(
      value => {
        const next = {lat:value.coords.latitude,lon:value.coords.longitude};
        if (next.lat < -34.2 || next.lat > -33.5 || next.lon < 150.8 || next.lon > 151.5) {
          setError('当前位置超出当前支持的悉尼范围。');setLocating(false);return;
        }
        setPosition(next);setOrigin('current');setLocating(false);
      },
      () => {setError('未能获取当前位置；请允许位置权限，或选择一个固定起点。');setLocating(false);},
      {enableHighAccuracy:false,timeout:10000,maximumAge:300000},
    );
  }

  function watch(created: Run, ticket: number) {
    if (ticket !== generation.current) return;
    setRun(created);
    source.current?.close();
    const events = new EventSource(`/api/runs/${created.id}/events`);
    source.current = events;
    const complete = (value: Run) => {
      if (ticket !== generation.current) return;
      events.close(); setRun(value); setBusy(false);
      if (value.result) {
        setResult(value.result);setSelected(0);setRerollOpen(false);scrollToQuest();
        const said = value.result.trace.find(t => t.action === 'degrade') ?? value.result.trace.find(t => t.action === 'taste_miss');
        if (said) setNotice(said.summary);
      }
      else setError(value.status === 'cancelled' ? '这次已取消。' : '没能生成支线，请重试。');
    };
    setProgress('');
    // Agent steps arrive while the model is still deciding; show the latest one.
    events.addEventListener('trace', event => {if (ticket === generation.current) setProgress(JSON.parse((event as MessageEvent).data).summary);});
    events.addEventListener('done', event => complete(JSON.parse((event as MessageEvent).data)));
    events.onerror = () => {
      events.close();
      api<Run>(`/runs/${created.id}`).then(value => {
        if (['queued','running'].includes(value.status)) {
          if (ticket === generation.current) {setBusy(false);setError('进度连接中断，可以再领一次。');}
        } else complete(value);
      }).catch(e => {if (ticket === generation.current) {setBusy(false);setError(e.message);}});
    };
  }

  async function start(request: PlanRequest) {
    const ticket = ++generation.current;
    source.current?.close();setBusy(true);setError('');setNotice('');setProgress('');setEvidenceOpen(false);
    try {
      watch(await api<Run>(`/runs?strategy=${strategy}`,request), ticket);
    } catch(e) {if (ticket === generation.current) {setError((e as Error).message);setBusy(false);}}
  }

  async function generate() {
    if (!session) return;
    if (!fitsToday) {setError('这段时间跨过了午夜。支线需要当天回来，缩短一点试试。');return;}
    if (savePreference && preference.trim()) {
      try {await api('/taste/notes', {text:preference.trim().slice(0,60)});await refreshTaste();setSavePreference(false);}
      catch(e) {setError((e as Error).message);return;}
    }
    setInferenceDismissed(false);setAdjustOpen(false);setProposal(null);
    start({origin_id:origin, origin_lat:origin==='current'?position?.lat??null:null, origin_lon:origin==='current'?position?.lon??null:null, departure:zoned(date,depart), deadline:zoned(date,endTime), preference, max_walk_minutes:walk, budget_aud:budget === '' ? null : Number(budget), include_transport_cost:includeTransportCost, max_stops:3, locked_ids:[], excluded_ids:[], stay_minutes:{}, mode, catalog:'osm', venue_facts:'advisory'});
  }

  async function revise(patch: Partial<PlanRequest>) {
    if (!run || !result || busy) return;
    const ticket = ++generation.current;
    setBusy(true);setError('');source.current?.close();
    try {watch(await api<Run>(`/runs/${run.id}/revise`,patch), ticket);}
    catch(e) {setError((e as Error).message);setBusy(false);}
  }

  // Reroll reasons are typed and scoped; the server applies them to the taste session,
  // excludes what was on screen, and re-plans. Only "先看看别的" stays client-side.
  function exclude(ids: string[]) {
    return [...new Set([...(result?.request.excluded_ids ?? []), ...ids])].filter(id => !result?.request.locked_ids.includes(id)).slice(-64);
  }
  async function reroll(reason: Reason, note = '') {
    if (!run || !plan || busy) return;
    const ticket = ++generation.current;
    setBusy(true);setError('');setNotice('');setProgress(reason === 'other' ? '正在理解这句话…' : '');setDetailOpen(false);setRerollOpen(false);source.current?.close();
    try {
      const response = await api<RerollResponse>(`/runs/${run.id}/reroll`, {itinerary_id:plan.id, reason, note});
      setOtherText('');
      if (reason === 'no_spend') setBudget('0');
      if (response.proposal) setProposal(response.proposal);
      if (response.message) setNotice(response.message);
      if (response.clarify === 'time' || !response.id) {setBusy(false);rerollTime();return;}
      watch({id:response.id, status:response.status ?? 'queued', result:null}, ticket);
      refreshTaste();
    } catch(e) {if (ticket === generation.current) {setError((e as Error).message);setBusy(false);}}
  }
  async function acceptQuest() {
    if (!run || !plan) return;
    setDetailOpen(!detailOpen);setRerollOpen(false);
    if (accepted.includes(plan.id)) return;
    setAccepted([...accepted, plan.id]);
    try {
      const response = await api<{recorded: boolean; proposal: Proposal | null}>(`/runs/${run.id}/accept`, {itinerary_id:plan.id});
      if (response.proposal) setProposal(response.proposal);
    } catch(e) {setError((e as Error).message);}
  }
  async function decide(target: Proposal, accept: boolean) {
    try {
      await api('/taste/proposals', {signature:target.signature, accept});
      if (proposal?.signature === target.signature) setProposal(null);
      if (accept) setNotice(`记住了：${target.text}。随时可以在「我的偏好」里删掉。`);
      await refreshTaste();
    } catch(e) {setError((e as Error).message);if (proposal?.signature === target.signature) setProposal(null);}
  }
  async function forget(path: string) {
    try {await api(path, {}, 'DELETE');await refreshTaste();} catch(e) {setError((e as Error).message);}
  }
  function rerollTime() {
    setRerollOpen(false);setAdjustOpen(true);
    window.scrollTo({top:0,behavior:'smooth'});
  }
  function rerollOther() {
    if (!result || !plan) return;
    const next = result.itineraries.findIndex((item, i) => i > selected && !sharesStop(item, plan));
    const any = next >= 0 ? next : result.itineraries.findIndex((_, i) => i > selected);
    if (any >= 0) {setSelected(any);setRerollOpen(false);setDetailOpen(false);setEvidenceOpen(false);return;}
    setDetailOpen(false);
    revise({excluded_ids:exclude(result.itineraries.flatMap(i => i.stops.map(s => s.candidate.id)))});
  }

  function modifyText() {
    const text = revisionText.trim(); if (!text || !result) return;
    const match = text.match(/(\d{1,2})[:：](\d{2})\s*前/);
    if (match) revise({deadline:zoned(result.request.departure.slice(0,10),`${match[1].padStart(2,'0')}:${match[2]}`)});
    else {revise({preference:text});setNotice('按关键词调整了这次的偏好；更复杂的描述还理解不了。');}
    setRevisionText('');
  }
  async function saveMemory() {
    if (!memoryText.trim()) return;
    try {await api('/taste/notes',{text:memoryText.trim()});setMemoryText('');await refreshTaste();}
    catch(e) {setError((e as Error).message);}
  }

  const req = result?.request;
  const questMinutes = req ? Math.round((new Date(req.deadline).getTime() - new Date(req.departure).getTime()) / 60000) : minutes;
  const slot = req && !inferenceDismissed ? carvedFrom(req.departure.slice(0,10), toMinutes(time(req.departure)), questMinutes) : null;
  const live = req?.mode === 'live';
  // Runs stored before advisory mode was added may not contain either array.
  const unknowns = plan?.unknowns ?? [];
  const advisories = plan?.advisories ?? [];

  return <>
    <header className="topbar">
      <a className="brand" href="/" aria-label="Sidequest 首页"><span className="brand-mark">!</span>sidequest</a>
      <nav>
        <button className="text-button" onClick={() => {setMemoryOpen(true);refreshTaste();}}><SlidersHorizontal size={14}/> 我的偏好{!!taste?.proposals.length && <span className="dot" aria-label={`${taste.proposals.length} 条待确认`}/>}</button>
        <button className="icon-button" aria-label="打开开发视图" title="开发视图" onClick={() => setTraceOpen(true)}><Code2 size={17}/></button>
      </nav>
    </header>

    <main>
      <section className={`intake ${result || busy ? 'compact' : ''}`} aria-label="领取支线">
        <p className="kicker">主线之外</p>
        <h1>空出来的这点时间，<br/>拿去做点什么。</h1>
        <p className="lede">说一句你现在的状态。给你一个附近真实存在、时间上赶得回来的支线任务。</p>

        <div className="intake-card">
          <textarea ref={inputRef} rows={2} aria-label="用一句话说说现在" placeholder="下午空出来了，不想待在家…" maxLength={300} value={preference}
            onChange={e => setPreference(e.target.value)} onKeyDown={e => {if (e.key === 'Enter' && !e.shiftKey) {e.preventDefault();generate();}}}/>
          {!preference && <div className="examples">{examples.map(t => <button key={t} onClick={() => {setPreference(t);inputRef.current?.focus();}}>{t}</button>)}</div>}

          <div className="picks">
            <div className="pick-group" role="group" aria-label="什么时候出发">
              {([['now','现在'],['tonight','今晚'],['tomorrow','明天']] as [When,string][]).map(([k,l]) =>
                <button key={k} className={when===k?'on':''} aria-pressed={when===k} onClick={() => pickWhen(k)}>{l}</button>)}
              {when==='custom' && <button className="on" aria-pressed onClick={() => setAdjustOpen(true)}>{depart}</button>}
            </div>
            <div className="pick-group" role="group" aria-label="有多少时间">
              {durations.map(([m,l]) => <button key={m} className={minutes===m?'on':''} aria-pressed={minutes===m} onClick={() => setMinutes(m)}>{l}</button>)}
            </div>
          </div>

          <div className="intake-foot">
            <button className="assumption" aria-expanded={adjustOpen} onClick={() => setAdjustOpen(!adjustOpen)}>
              {date === session?.tomorrow ? '明天' : '今天'} {depart} 从 <b>{names[origin]}</b> 出发 · {fitsToday ? <><b>{endTime}</b> 前回来</> : <span className="warn">会跨过午夜</span>}
              <ChevronDown size={13} className={adjustOpen ? 'rotate' : ''}/>
            </button>
            <button className="go" disabled={busy || !session || !fitsToday} onClick={generate}>
              {busy ? <LoaderCircle className="spin" size={16}/> : null}{busy ? '正在找' : '领一个支线'}{!busy && <ArrowRight size={16}/>}
            </button>
          </div>

          {adjustOpen && <div className="adjust">
            <label>起点<select value={origin} onChange={e => setOrigin(e.target.value as Origin)}>{position&&<option value="current">当前位置</option>}<option value="townhall">Town Hall</option><option value="central">Central Station</option><option value="circular">Circular Quay</option></select>
              <button type="button" className="link" disabled={locating} onClick={useCurrentLocation}><LocateFixed size={11}/>{locating?'定位中':'用当前位置'}</button></label>
            <label>日期<select value={date} onChange={e => {setDate(e.target.value);setWhen('custom');}}>{session && <><option value={session.today}>今天</option><option value={session.tomorrow}>明天</option></>}</select></label>
            <label>出发<input aria-label="出发时间，悉尼当地时间" type="time" value={depart} onChange={e => {setDepart(e.target.value);setWhen('custom');}}/></label>
            <label>全程最多步行<span className="with-unit"><input type="number" min="0" max="480" value={walk} onChange={e => setWalk(Number(e.target.value))}/>分钟</span></label>
            <label>门票预算<span className="with-unit">AUD<input type="number" min="0" max="10000" placeholder="不限" value={budget} onChange={e => setBudget(e.target.value)}/></span></label>
            {budget!=='' && <label className="check"><input type="checkbox" checked={includeTransportCost} onChange={e => setIncludeTransportCost(e.target.checked)}/>预算含交通费</label>}
            {session?.agent_available && <label>推荐方式<select value={strategy} onChange={e=>setStrategy(e.target.value as 'agent'|'fixed')}><option value="agent">模型推荐</option><option value="fixed">固定策略（不调用模型）</option></select></label>}
            <label>数据<select value={mode} onChange={e=>setMode(e.target.value as PlanRequest['mode'])}><option value="replay">合成回放</option>{session?.available_modes.includes('live')&&<option value="live">Live · TfNSW</option>}</select></label>
            <label className="check"><input type="checkbox" checked={savePreference} onChange={e => setSavePreference(e.target.checked)}/>把这句话记成笔记（前 60 字）</label>
          </div>}
        </div>

        <p className="data-note">{mode==='replay' ? '地点来自 OpenStreetMap 快照；路线是合成回放，营业时间和费用未核实，别据此直接出门。' : '地点来自 OpenStreetMap 快照，路线来自 TfNSW；营业时间、费用和交通票价可能未知，出门前再看一眼。'}</p>
        {error && <div className="banner error" role="alert">{error}<button aria-label="关闭提示" onClick={() => setError('')}><X size={14}/></button></div>}
      </section>

      {(result || busy) && <section className="quest-area" ref={questRef} aria-busy={busy}>
        {busy && <div className="working" role="status"><LoaderCircle className="spin" size={16}/>{progress || '正在核对地点、路线和回程…'}
          <button className="text-button" onClick={async () => {generation.current++;source.current?.close();setBusy(false);if(run) await api(`/runs/${run.id}/cancel`,{}).catch(e=>setError(e.message));}}>取消</button></div>}
        {notice && <div className="banner" role="status">{notice}<button aria-label="关闭提示" onClick={() => setNotice('')}><X size={14}/></button></div>}
        {proposal && <div className="proposal" role="status">
          <p className="proposal-kicker">{proposal.action==='add'?'要不要记住':proposal.action==='narrow'?'要不要改一下':'要不要忘掉'}</p>
          <p className="proposal-text">{proposal.text}</p>
          <p className="proposal-basis">{proposal.evidence ? `依据你 ${proposal.evidence} 次的选择。` : '来自你刚写的那句话。'}不确认就不会记。</p>
          <div><button className="go" onClick={() => decide(proposal, true)}>{proposal.action==='retire'?'忘掉':'记住'}</button><button className="ghost" onClick={() => decide(proposal, false)}>不用</button></div>
        </div>}

        {result && !plan && !busy && <div className="empty">
          <h2>这次没找到赶得回来的支线</h2>
          <p>{result.message}</p>
          <div><button className="ghost" onClick={rerollTime}>换个时间</button><button className="text-button" onClick={() => setTraceOpen(true)}>看看哪些被排除了 <ArrowRight size={13}/></button></div>
        </div>}

        {plan && req && <article className={`quest ${busy ? 'dim' : ''}`}>
          <header className="quest-head">
            <span className="quest-no">支线 {String(selected + 1).padStart(2,'0')}</span>
            <span className={`stamp ${plan.status}`}>{plan.status==='verified' ? <><Check size={12}/>{advisories.length ? '时间可行' : '已核实'}</> : <>? 有待核实</>}</span>
          </header>

          <p className="carved">
            {slot ? <>从<em>{slot}</em>里偷出来的 {duration(questMinutes)}<button className="inferred" title="这是按时间猜的，点一下去掉" onClick={() => setInferenceDismissed(true)}>推断 <X size={10}/></button></>
              : <>主线之外的 {duration(questMinutes)}</>}
          </p>
          <h2 className="quest-title">{plan.title}</h2>
          <p className="brief">
            {time(req.departure)} 从 {names[req.origin_id]} 出发，
            {plan.stops.map((s,i) => <React.Fragment key={s.candidate.id}>{i === 0 ? '先去 ' : i === plan.stops.length - 1 ? '，最后到 ' : '，顺路 '}<b>{s.candidate.name}</b></React.Fragment>)}
            ，{time(plan.return_at)} 前回到原地。
          </p>

          <div className="quest-body">
            <section>
              <h3>去做什么</h3>
              <ol className="stops">{plan.stops.map((s,i) => <li key={s.candidate.id}>
                <span className="n">{i+1}</span>
                <div><div className="stop-line"><b>{s.candidate.name}</b><span>{time(s.start)}–{time(s.end)} · {s.stay_minutes} 分钟</span></div><p>{s.candidate.description}</p></div>
              </li>)}</ol>
            </section>
            <section>
              <h3>为什么可能适合你</h3>
              <p>{req.preference.trim() || result?.agent ? plan.reason : '你还没说想要什么，所以这次说不出它为什么适合你——它只是在这段时间里刚好走得通。'}</p>
            </section>
            <section className="facts">
              <div><span>路上</span><b>步行 {plan.walking_minutes} 分钟{plan.legs.some(l => l.mode === 'transit') ? ' + 公共交通' : ''}</b></div>
              <div><span>已知花费</span><b>{plan.known_cost === 0 ? 'AUD 0（另有未知项）' : `AUD ${plan.known_cost}`}</b></div>
              <div><span>回到原地</span><b>{time(plan.return_at)}</b></div>
            </section>
            {plan.status === 'verified'
              ? <p className="assurance">路线和时间预算已核实，按当前估算赶得回来。</p>
              : <div className="unknowns"><h3>还没核实的</h3><ul>{[...unknowns,...advisories].map((u,i) => <li key={i}>{u}</li>)}</ul></div>}
            {plan.status === 'verified' && (unknowns.length > 0 || advisories.length > 0) && <div className="unknowns"><h3>出发前确认</h3><ul>{[...advisories,...unknowns].map((u,i) => <li key={i}>{u}</li>)}</ul></div>}
          </div>

          <footer className="quest-actions">
            <button className="go" disabled={busy} onClick={acceptQuest} aria-expanded={detailOpen}>{detailOpen ? '收起安排' : '就这个'}<ChevronDown size={15} className={detailOpen ? 'rotate' : ''}/></button>
            <button className="ghost" disabled={busy} onClick={() => {setRerollOpen(!rerollOpen);setDetailOpen(false);}} aria-expanded={rerollOpen}><RefreshCw size={14}/>换一个</button>
          </footer>

          {rerollOpen && <div className="reroll">
            <p>为什么想换？</p>
            <div role="group" aria-label="口味">{tasteChips.map(([reason,label]) => <button key={reason} disabled={busy} onClick={() => reroll(reason)}>{label}</button>)}</div>
            <div role="group" aria-label="这类地方或这个地方">
              {anchor && kindOf(anchor.tags) && <button disabled={busy} onClick={() => reroll('not_this_kind')}>不想要这类</button>}
              <button disabled={busy} onClick={() => reroll('never_here')}>以后别推 {anchor?.name}</button>
              {result?.agent && plan.stops.length > 1 && <button disabled={busy} onClick={() => reroll('off_route')}>保留 {anchor?.name}，换掉顺路的站</button>}
            </div>
            <div role="group" aria-label="其他">
              <button disabled={busy} onClick={() => reroll('been_there')}>去过了</button>
              <button disabled={busy} onClick={() => reroll('no_spend')}>不想花钱</button>
              <button disabled={busy} onClick={rerollTime}>时间不合适</button>
              <button disabled={busy} onClick={rerollOther}>先看看别的</button>
            </div>
            {result?.agent && <form className="refine other" onSubmit={e => {e.preventDefault();if (otherText.trim()) reroll('other', otherText.trim());}}>
              <input aria-label="其他理由" value={otherText} maxLength={100} onChange={e=>setOtherText(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();if(otherText.trim()) reroll('other', otherText.trim());}}} placeholder="其他：用一句话说说为什么想换"/>
              <button aria-label="带着这句话换一个" disabled={busy||!otherText.trim()}><ArrowRight size={15}/></button>
            </form>}
          </div>}

          {detailOpen && <div className="plan">
            <ol className="timeline">
              <li className="tl-end"><b>{time(req.departure)}</b><span>从 {names[req.origin_id]} 出发</span></li>
              {plan.stops.map((stop,i) => <React.Fragment key={stop.candidate.id}>
                <li className="tl-leg">{plan.legs[i].mode==='walk'?<Footprints size={13}/>:<TrainFront size={13}/>}{plan.legs[i].mode==='walk'?'步行':'公共交通'} {plan.legs[i].minutes} 分钟<small>{live?'TfNSW':'回放'}</small></li>
                <li className="tl-stop">
                  <div className="tl-row">
                    <div><b>{time(stop.start)}</b><span className="tl-name">{stop.candidate.name}</span></div>
                    <div className="tl-actions">
                      <button disabled={busy} className={`icon-button ${stop.locked?'locked':''}`} aria-label={`${stop.locked?'解锁':'锁定'} ${stop.candidate.name}`} title={stop.locked?'解锁这站':'锁定这站'} onClick={() => revise({locked_ids:stop.locked?req.locked_ids.filter(id=>id!==stop.candidate.id):[...req.locked_ids,stop.candidate.id]})}>{stop.locked?<LockKeyhole size={14}/>:<UnlockKeyhole size={14}/>}</button>
                      <button disabled={busy||stop.locked} className="icon-button" aria-label={`替换 ${stop.candidate.name}`} title="换掉这站" onClick={() => revise({excluded_ids:exclude([stop.candidate.id])})}><RefreshCw size={13}/></button>
                      <button disabled={busy||stop.locked||plan.stops.length<=1} className="icon-button" aria-label={`删除 ${stop.candidate.name}`} title="去掉这站" onClick={() => revise({excluded_ids:exclude([stop.candidate.id]),max_stops:Math.max(1,plan.stops.length-1)})}><Trash2 size={13}/></button>
                    </div>
                  </div>
                  <div className="tl-tags"><span>停留 {stop.stay_minutes} 分钟 · {stop.stay_basis}</span>{stop.wait_minutes>0&&<span>到了先等 {stop.wait_minutes} 分钟</span>}<button disabled={busy} onClick={() => revise({stay_minutes:{...req.stay_minutes,[stop.candidate.id]:Math.min(300,stop.stay_minutes+15)}})}><Plus size={10}/>多留 15 分钟</button></div>
                </li>
              </React.Fragment>)}
              <li className="tl-leg">{plan.legs.at(-1)?.mode==='transit'?<TrainFront size={13}/>:<Footprints size={13}/>}回程 {plan.legs.at(-1)?.minutes} 分钟<small>{live?'TfNSW':'回放'}</small></li>
              <li className="tl-end"><b>{time(plan.return_at)}</b><span>回到 {names[req.origin_id]}</span><Check size={14}/></li>
            </ol>

            <div className="refine">
              <input aria-label="调整这趟" value={revisionText} maxLength={300} onChange={e=>setRevisionText(e.target.value)} onKeyDown={e=>{if(e.key==='Enter') modifyText();}} placeholder="比如“13:00前回来”"/>
              <button aria-label="应用" disabled={busy||!revisionText.trim()} onClick={modifyText}><ArrowRight size={15}/></button>
            </div>
            <div className="refine-chips">
              <button disabled={busy||plan.stops.length<=1} onClick={()=>revise({max_stops:Math.max(1,plan.stops.length-1)})}>少去一站</button>
              <button disabled={busy} onClick={()=>revise({deadline:new Date(new Date(req.deadline).getTime()-30*60000).toISOString()})}>早半小时回来</button>
            </div>

            <button className="evidence-toggle" aria-expanded={evidenceOpen} onClick={()=>setEvidenceOpen(!evidenceOpen)}>凭什么说走得通 <ChevronDown size={14} className={evidenceOpen ? 'rotate' : ''}/></button>
            {evidenceOpen && <div className="evidence">
              {plan.checks.map((c,i) => <div key={i} className={`check ${c.status}`}><span>{c.status==='pass'?'✓':c.status==='unknown'?'?':'×'}</span><p>{c.detail}</p></div>)}
              <p className="source">{live?'地点来源：OpenStreetMap 快照；路线来源：TfNSW。':'地点来源：OpenStreetMap 快照；路线来源：本地合成回放。'}</p>
            </div>}
            <a className="text-button map-link" target="_blank" rel="noreferrer" href={plan.map_url}>在 Google Maps 打开整条路线 <ExternalLink size={12}/></a>
          </div>}
        </article>}
      </section>}
    </main>

    {(traceOpen||memoryOpen)&&<div className="drawer-backdrop" onClick={()=>{setTraceOpen(false);setMemoryOpen(false);}}><section className="drawer" role="dialog" aria-modal="true" aria-label={traceOpen?'开发视图':'我的偏好'} onClick={e=>e.stopPropagation()}>
      <div className="drawer-heading"><h2>{traceOpen?'开发视图':'我的偏好'}</h2><button autoFocus className="icon-button" aria-label="关闭面板" onClick={()=>{setTraceOpen(false);setMemoryOpen(false);}}><X size={18}/></button></div>
      {traceOpen?<>
        <p className="drawer-description">{result?.agent ? `模型推荐 agent-v1 · 模型决定去哪，执行器决定去不去得了。随机种子 ${result.agent.seed}。` : `${result?.strategy ?? '固定策略 fixed-v1'} · 不调用模型。`}下面是可审计的动作摘要。</p>
        {result?.agent?.probe && <p className="drawer-description">押注：{result.agent.probe.dimension ?? '不试探'}{result.agent.probe.dimension ? ` → ${result.agent.probe.pole}` : ''}（{result.agent.probe.mode}）{result.agent.memory_ids.length ? ` · 用到 ${result.agent.memory_ids.length} 条记忆` : ''}</p>}
        <div className="trace-metrics">{result?.agent ? <><div><strong>{result.agent.model_calls}</strong><span>模型调用</span></div><div><strong>{result.agent.tokens}</strong><span>Token</span></div></> : <><div><strong>{result?.tool_calls??'—'}<small>/20</small></strong><span>Provider 请求</span></div><div><strong>{result?.cache_hits??'—'}</strong><span>缓存命中</span></div></>}<div><strong>{result?.elapsed_ms??'—'}<small>ms</small></strong><span>服务端耗时</span></div></div>
        {result&&result.itineraries.length>1&&<p className="drawer-description">本次共 {result.itineraries.length} 个可行方案，主界面一次只展示一个。{result.message}</p>}
        {run&&<div className="run-label">Run {run.id.slice(0,12)} · {run.status}<br/>{result?.agent ? `模型调用 ${result.agent.model_calls} · ${live?'TfNSW Live':'合成回放'}` : live?'TfNSW Live · 模型调用 0':'外部请求 0 · 模型调用 0 · 合成回放'}</div>}
        {result?.trace.map(t=><div className="trace-row" key={t.sequence}><span className="trace-number">{String(t.sequence).padStart(2,'0')}</span><div><code>{t.action}</code><p>{t.summary}</p></div><small>{t.elapsed_ms}ms{t.cache_hit&&' · cache'}</small></div>)}
        {!result&&<div className="empty-drawer">领一次支线后，这里会显示调用和验证记录。</div>}
        {!!result?.rejected.length&&<div className="rejections"><h3>没有通过的组合</h3>{result.rejected.map((r,i)=><div key={i}><code>{r.candidates.join(' → ')}</code><p>{r.reasons.join('；')}</p></div>)}</div>}
      </>:<>
        <p className="drawer-description">同一个意思在不止一次出门里反复出现，系统才会提议记住；不确认就不会记。你当下说的话永远优先。</p>
        <label className="incognito"><input type="checkbox" checked={!!taste?.incognito} onChange={async e => {try {await api('/taste/settings',{incognito:e.target.checked},'PUT');await refreshTaste();} catch(err) {setError((err as Error).message);}}}/>暂停记录经历<span>打开期间换一个、就这个都不会留下记录</span></label>
        {!!taste?.proposals.length && <div className="pending"><h3>等你确认</h3>{taste.proposals.map(p => <div className="pending-item" key={p.signature}><p>{p.text}</p><span>{p.evidence ? `依据 ${p.evidence} 次经历` : '来自你写的一句话'}</span><div><button className="go" onClick={() => decide(p, true)}>{p.action==='retire'?'忘掉':'记住'}</button><button className="ghost" onClick={() => decide(p, false)}>不用</button></div></div>)}</div>}
        <h3 className="drawer-section">记住的</h3>
        {!taste?.items.length && <div className="empty-drawer">还没有记住什么。</div>}
        {taste?.items.map(m => <div className="memory-item" key={m.id}>
          <p>{m.text}</p>
          <div className="memory-tags">{m.context ? <span className="ctx">{m.context}</span> : <span className="ctx">所有时候</span>}<span>{sourceLabel[m.source]}</span>{m.source==='agent_proposed'&&<span>{m.kind==='note' ? '来自你写的一句话' : `依据 ${m.evidence} 次经历`}</span>}{m.stale&&<span className="stale">最近没再用上</span>}</div>
          <div>{m.kind==='note'&&<button onClick={()=>{setPreference(m.text);setMemoryOpen(false);}}>用这句</button>}<button aria-label={`删除记忆 ${m.text}`} onClick={() => forget(`/taste/items/${m.id}`)}><Trash2 size={13}/> 删除</button></div>
        </div>)}
        <div className="memory-add"><input aria-label="添加一条笔记" value={memoryText} maxLength={60} onChange={e=>setMemoryText(e.target.value)} onKeyDown={e=>{if(e.key==='Enter') saveMemory();}} placeholder="写一句笔记，例如：不喜欢要排队拍照的地方"/><button className="go" onClick={saveMemory} disabled={!memoryText.trim()}><Plus size={15}/>添加</button></div>
        <button className="evidence-toggle" aria-expanded={episodesOpen} onClick={()=>setEpisodesOpen(!episodesOpen)}>最近的经历（{taste?.episodes.length ?? 0}） <ChevronDown size={14} className={episodesOpen ? 'rotate' : ''}/></button>
        {episodesOpen && <div className="episodes">
          {!taste?.episodes.length && <p className="drawer-description">还没有记录。</p>}
          {taste?.episodes.map(e => <div className="episode" key={e.id}><div><b>{e.reason}</b><span>{e.context} · {new Date(e.created_at).toLocaleDateString('zh-CN')}{!e.counts&&' · 不算口味'}</span></div><button aria-label={`删除经历 ${e.reason}`} onClick={() => forget(`/taste/episodes/${e.id}`)}><Trash2 size={13}/></button></div>)}
          <p className="privacy-note">删掉一条经历，靠它才成立的记忆会一起失效。</p>
        </div>}
        <p className="privacy-note">用随机会话标识隔离，不需要账号。别在这里存住址之类的敏感信息。</p>
      </>}
    </section></div>}
  </>;
}

createRoot(document.getElementById('root')!).render(<App/>);
