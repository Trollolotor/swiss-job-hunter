import { useCallback, useEffect, useState } from "react";

const box={background:"#f0f3ed",border:"1px solid #d4dece",borderRadius:6,padding:14};
const input={width:"100%",padding:"7px",border:"1px solid #c8d8c4",borderRadius:4,fontSize:12,boxSizing:"border-box"};
const button={padding:"6px 10px",border:"1px solid #2e7d5250",borderRadius:4,background:"#fff",color:"#24603e",cursor:"pointer"};
const date=v=>v?new Date(v+(/Z$|[+]\d\d:/.test(v)?"":"Z")).toLocaleString():"Never";

export default function EmployerSourcesView({api}) {
  const [settings,setSettings]=useState(null);
  const [employers,setEmployers]=useState([]);
  const [operations,setOperations]=useState({tasks:[],incidents:[],budget:[]});
  const [detail,setDetail]=useState(null);
  const [message,setMessage]=useState("");
  const [query,setQuery]=useState("");
  const [offset,setOffset]=useState(0);
  const [name,setName]=useState("");
  const [connection,setConnection]=useState({employer_id:"",official_url:"",portal_url:""});
  const [prices,setPrices]=useState("{}");
  const [busy,setBusy]=useState(false);
  const request=useCallback(async(path,body,method="POST")=>{
    const r=await fetch(api+path,body===undefined&&method==="GET"?undefined:{method,headers:{"Content-Type":"application/json"},...(body!==undefined?{body:JSON.stringify(body)}:{})});
    const value=await r.json();
    if(!r.ok)throw new Error(typeof value.detail==="string"?value.detail:JSON.stringify(value.detail));
    return value;
  },[api]);
  const refresh=useCallback(async()=>{
    const [e,o]=await Promise.all([request(`/employers?q=${encodeURIComponent(query)}&offset=${offset}`,undefined,"GET"),request("/employers/operations",undefined,"GET")]);
    setEmployers(e);setOperations(o);
  },[request,query,offset]);
  useEffect(()=>{
    request("/settings/employer-sources",undefined,"GET").then(s=>{setSettings(s);setPrices(JSON.stringify(s.prices,null,2));}).catch(e=>setMessage(e.message));
  },[request]);
  useEffect(()=>{
    refresh().catch(e=>setMessage(e.message));
    const timer=setInterval(()=>refresh().catch(e=>setMessage(e.message)),10000);
    return()=>clearInterval(timer);
  },[refresh]);
  const action=async(fn)=>{
    setBusy(true);setMessage("");
    try{await fn();await refresh();}catch(e){setMessage(e.message);}finally{setBusy(false);}
  };
  const save=()=>action(async()=>{
    const {schema_ready,runner_configured,launcher_available,...value}=settings;
    const result=await request("/settings/employer-sources",{...value,prices:JSON.parse(prices)},"PUT");
    setSettings(result);setMessage("Employer-source settings saved");
  });
  const sourceAction=(id,verb)=>action(async()=>{
    const result=await request(`/employer-sources/${id}/${verb}`);
    setMessage(result.task_id?`Queued task #${result.task_id}`:"Source paused");
    if(detail?.id===id)setDetail(await request(`/employer-sources/${id}`,undefined,"GET"));
  });
  if(!settings)return <div style={box}>{message||"Loading employer sources…"}</div>;
  return <div style={{display:"grid",gap:14}}>
    <section style={box}>
      <h3 style={{marginTop:0}}>Employer sources</h3>
      <p style={{fontSize:12}}>Discover employers from board results and monitor verified career portals.</p>
      <label><input type="checkbox" checked={settings.enabled} onChange={e=>setSettings({...settings,enabled:e.target.checked})}/> Enable employer sources</label>
      <label style={{display:"block",marginTop:8}}><input type="checkbox" checked={settings.shadow} onChange={e=>setSettings({...settings,shadow:e.target.checked})}/> Preview collection without changing vacancies</label>
      <label style={{display:"block",marginTop:12}}>Vacancy coverage
        <select style={input} value={settings.coverage} onChange={e=>setSettings({...settings,coverage:e.target.value})}>
          <option value="swiss">Swiss locations</option>
          <option value="swiss_remote">Swiss locations and remote roles eligible from Switzerland</option>
          <option value="worldwide">Worldwide</option>
        </select>
      </label>
      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:10,marginTop:10}}>
        {[["poll_hours","Collection interval (hours)"],["onboarding_per_day","New employers per day"],["max_connected","Connected-source limit"],["concurrency","Concurrent collections"],["daily_budget_usd","Daily onboarding and repair budget ($)"],["max_task_usd","Maximum cost per LLM attempt ($)"]].map(([key,label])=><label key={key} style={{fontSize:11}}>{label}<input style={input} type="number" min="0" step={key.includes("usd")?"0.1":"1"} value={settings[key]??""} placeholder={key==="daily_budget_usd"?"Set before paid onboarding":""} onChange={e=>setSettings({...settings,[key]:e.target.value===""?null:Number(e.target.value)})}/></label>)}
      </div>
      {!settings.daily_budget_usd&&<p style={{fontSize:11}}>Paid discovery and repairs wait until a budget is configured. Working connectors can continue collecting.</p>}
      <details style={{margin:"12px 0",fontSize:11}}><summary>Advanced limits and model prices</summary>
        <p>Model prices use USD per million tokens. Search price is USD per request. Example: {`{"model/id":{"input_per_million":1,"output_per_million":5,"search_request":0.007}}`}</p>
        <textarea aria-label="Employer model prices" style={{...input,minHeight:130,fontFamily:"monospace"}} value={prices} onChange={e=>setPrices(e.target.value)}/>
        {[["max_requests","Requests per source run"],["max_pages","Pages per catalogue"],["runtime_seconds","Maximum runtime (seconds)"],["domain_delay_seconds","Minimum domain request spacing (seconds)"],["repair_budget_fraction","Budget fraction reserved for repairs"]].map(([key,label])=><label key={key} style={{display:"block",marginTop:6}}>{label}<input style={input} type="number" step="any" value={settings[key]} onChange={e=>setSettings({...settings,[key]:Number(e.target.value)})}/></label>)}
        <p>Migration: {settings.schema_ready?"ready":"required"} · Isolated runtime: {settings.runner_configured&&settings.launcher_available?"configured":"not configured"}</p>
      </details>
      <div style={{display:"flex",gap:8}}><button disabled={busy} style={button} onClick={save}>Save settings</button><button disabled={busy} style={button} onClick={()=>action(async()=>{const r=await request("/employer-sources/backfill");setMessage(`Registry backfill queued #${r.task_id}`);})}>Import stored employers</button></div>
    </section>
    <section style={box}>
      <h3 style={{marginTop:0}}>Employers and portals</h3>
      <form onSubmit={e=>{e.preventDefault();action(async()=>{const value=await request("/employers",{name});setName("");setConnection({...connection,employer_id:String(value.id)});});}} style={{display:"flex",gap:6}}><input aria-label="Employer name" style={input} placeholder="Add an employer" value={name} onChange={e=>setName(e.target.value)} required/><button style={button} disabled={busy}>Add</button></form>
      <input aria-label="Search employers" style={{...input,margin:"10px 0"}} placeholder="Search discovered employers" value={query} onChange={e=>{setQuery(e.target.value);setOffset(0);}}/>
      {employers.map(employer=><div key={employer.id} style={{padding:"10px 0",borderTop:"1px solid #d4dece",fontSize:12}}>
        <b>{employer.name}</b> · {employer.resolution_status} · {employer.classification}
        <div style={{display:"flex",gap:6,marginTop:6}}><button style={button} disabled={busy} onClick={()=>action(async()=>{const r=await request(`/employers/${employer.id}/resolve`);setMessage(`Resolution queued #${r.task_id}`);})}>Resolve portal</button><button style={button} onClick={()=>setConnection({...connection,employer_id:String(employer.id)})}>Connect portal</button></div>
        {employer.sources.map(source=><div key={source.id} style={{marginTop:8,padding:9,background:"#fff",borderRadius:4}}>
          <a href={source.portal_url} target="_blank" rel="noreferrer">{source.platform} · {source.tenant}</a> · {source.enabled?source.health:"paused"}
          <div style={{fontSize:11,margin:"5px 0"}}>Last successful collection: {date(source.last_success_at)} · Next: {date(source.next_due_at)}</div>
          {source.last_error&&<p style={{color:"#a33",overflowWrap:"anywhere"}}>{source.last_error}</p>}
          <div style={{display:"flex",gap:5,flexWrap:"wrap"}}>
            {(source.enabled?["pause","collect","retry"]:["resume"]).map(verb=><button key={verb} disabled={busy} style={button} onClick={()=>sourceAction(source.id,verb)}>{verb}</button>)}
            <button style={button} onClick={()=>action(async()=>setDetail(await request(`/employer-sources/${source.id}`,undefined,"GET")))}>Diagnostics</button>
          </div>
        </div>)}
      </div>)}
      {!employers.length&&<p style={{fontSize:12}}>No employers found. Board searches populate this registry when the mode is enabled.</p>}
      <div style={{display:"flex",gap:6}}><button style={button} disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-50))}>Previous</button><button style={button} disabled={employers.length<50} onClick={()=>setOffset(offset+50)}>Next</button></div>
      {connection.employer_id&&<form style={{marginTop:15,display:"grid",gap:7}} onSubmit={e=>{e.preventDefault();action(async()=>{const r=await request(`/employers/${connection.employer_id}/connect`,{official_url:connection.official_url,portal_url:connection.portal_url});setMessage(`Portal verified; onboarding queued #${r.task_id}`);setConnection({employer_id:"",official_url:"",portal_url:""});});}}>
        <b>Verify portal for employer #{connection.employer_id}</b>
        <input style={input} type="url" aria-label="Official careers page" placeholder="Official page containing the career-portal link" required value={connection.official_url} onChange={e=>setConnection({...connection,official_url:e.target.value})}/>
        <input style={input} type="url" aria-label="Career portal URL" placeholder="Career portal URL" required value={connection.portal_url} onChange={e=>setConnection({...connection,portal_url:e.target.value})}/>
        <button style={button} disabled={busy}>Verify and queue connection</button>
      </form>}
    </section>
    <section style={box}>
      <h3 style={{marginTop:0}}>Recent operations</h3>
      {operations.metrics&&<p style={{fontSize:11}}>
        Direct vacancies: {operations.metrics.direct_vacancies} · Direct only: {operations.metrics.direct_only_vacancies} · Relevant: {operations.metrics.relevant_direct_vacancies} · Today’s cost/reservations: ${operations.metrics.daily_cost_upper_bound_usd.toFixed(3)}
      </p>}
      {operations.tasks.slice(0,10).map(t=><div key={t.id} style={{fontSize:11,padding:"5px 0"}}>#{t.id} {t.kind} · {t.status}{t.error&&<div style={{color:"#a33"}}>{t.error}</div>}</div>)}
      {detail&&<details open style={{fontSize:11,marginTop:10}}><summary>Source #{detail.id} diagnostics</summary>
        <button style={{...button,margin:"8px 0"}} disabled={busy} onClick={()=>sourceAction(detail.id,"rollback")}>Validate previous version for rollback</button>
        <pre style={{whiteSpace:"pre-wrap",maxHeight:350,overflow:"auto"}}>{JSON.stringify({runs:detail.runs,versions:detail.versions.map(({code,...v})=>v)},null,2)}</pre>
      </details>}
    </section>
    {message&&<div role="status" style={{...box,fontSize:12,overflowWrap:"anywhere"}}>{message}</div>}
  </div>;
}
