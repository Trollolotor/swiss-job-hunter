import { useEffect, useState } from "react";
import TagEditor from "./TagEditor";

const box={background:"#f0f3ed",border:"1px solid #d4dece",borderRadius:6,padding:16};
const input={width:"100%",padding:"7px 9px",border:"1px solid #c8d8c4",borderRadius:4,fontFamily:"monospace",fontSize:11};
const button={padding:"7px 12px",border:"1px solid #2e7d5240",borderRadius:4,background:"#2e7d5212",color:"#2e7d52",fontFamily:"monospace",fontWeight:700,cursor:"pointer"};
const OPERATIONS=["cv_parsing","keyword_extraction","job_screening","company_summary","translation","cv_tailoring","cover_letter"];
const SECTIONS=[
  ["about","About"],["experience","Experience"],["education","Education"],
  ["projects","Projects"],["skills","Skills"],["certifications","Certifications"],
  ["languages","Languages"],["publications","Publications"],["volunteering","Volunteering"],
  ["awards","Awards"],["additional","Additional"],
];
const emptySections=()=>Object.fromEntries(SECTIONS.map(([key])=>[key,""]));

export default function SettingsView({api,onProfilesChanged}) {
  const [profiles,setProfiles]=useState([]);
  const [selected,setSelected]=useState(null);
  const [llm,setLlm]=useState(null);
  const [priority,setPriority]=useState(null);
  const [message,setMessage]=useState("");
  const [expanded,setExpanded]=useState("about");
  const [preview,setPreview]=useState(null);
  const [parsing,setParsing]=useState(false);
  const [panel,setPanel]=useState("ai");
  const [prompts,setPrompts]=useState([]);
  const [promptKey,setPromptKey]=useState("");
  const [automation,setAutomation]=useState(null);
  const [runs,setRuns]=useState([]);
  const [promptHistory,setPromptHistory]=useState([]);

  const load=async()=>{
    const [p,l,pr,ps,au,ar]=await Promise.all([
      fetch(api+"/profiles").then(r=>r.json()),
      fetch(api+"/settings/llm").then(r=>r.json()),
      fetch(api+"/settings/search-priority").then(r=>r.json()),
      fetch(api+"/settings/prompts").then(r=>r.json()),
      fetch(api+"/settings/automation").then(r=>r.json()),
      fetch(api+"/automation/runs").then(r=>r.json()),
    ]);
    setProfiles(p);setSelected(old=>p.find(x=>x.id===old?.id)||p[0]||null);
    setLlm(l);setPriority(pr);setPrompts(ps);setPromptKey(old=>old||ps[0]?.key||"");setAutomation(au);setRuns(ar);
  };
  useEffect(()=>{load().catch(e=>setMessage(e.message));},[]);

  const saveProfile=async()=>{
    if(!selected)return;
    const exists=profiles.some(p=>p.id===selected.id);
    const r=await fetch(api+"/profiles"+(exists?"/"+selected.id:""),{
      method:exists?"PUT":"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(selected),
    });
    if(!r.ok)throw new Error(await r.text());
    setMessage("Profile saved");await load();onProfilesChanged?.();
  };
  const newProfile=()=>setSelected({id:"new-"+Date.now(),slug:"",name:"",keywords:[],default_location:"Zürich",cv_sections:emptySections(),active:true});
  const deleteProfile=async()=>{
    if(!selected||typeof selected.id!=="number")return;
    if(!confirm("Delete profile "+selected.name+"? Jobs will be kept."))return;
    await fetch(api+"/profiles/"+selected.id,{method:"DELETE"});
    setMessage("Profile deleted");await load();onProfilesChanged?.();
  };
  const parseFile=async(file)=>{
    if(!file)return;
    setParsing(true);setMessage("Parsing CV with OpenRouter…");
    try{
      const form=new FormData();form.append("file",file);
      const r=await fetch(api+"/profiles/parse-cv",{method:"POST",body:form});
      const d=await r.json();
      if(!r.ok)throw new Error(d.detail||JSON.stringify(d));
      setPreview(d);setMessage("CV parsed via "+d.model);
    }finally{setParsing(false);}
  };
  const saveLlm=async()=>{
    const r=await fetch(api+"/settings/llm",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(llm)});
    if(!r.ok)throw new Error(await r.text());setLlm(await r.json());setMessage("AI routing saved");
  };
  const testModel=async op=>{
    setMessage("Testing "+op+"…");
    const r=await fetch(api+"/settings/llm/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({operation:op})});
    const d=await r.json();setMessage(r.ok?"✓ "+d.model+": "+d.response:"✗ "+JSON.stringify(d));
  };
  const savePriority=async()=>{
    const r=await fetch(api+"/settings/search-priority",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(priority)});
    if(!r.ok)throw new Error(await r.text());setPriority(await r.json());setMessage("Priority settings saved");
  };
  const activePrompt=prompts.find(p=>p.key===promptKey);
  const updatePrompt=patch=>setPrompts(prompts.map(p=>p.key===promptKey?{...p,...patch}:p));
  const savePrompt=async()=>{
    const r=await fetch(api+"/settings/prompts/"+promptKey,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(activePrompt)});
    const d=await r.json();if(!r.ok)throw new Error(d.detail||JSON.stringify(d));
    updatePrompt(d);setMessage("Prompt saved and activated · revision "+d.revision);
  };
  const resetPrompt=async()=>{
    const r=await fetch(api+"/settings/prompts/"+promptKey+"/reset",{method:"POST"});
    const d=await r.json();if(!r.ok)throw new Error(d.detail||JSON.stringify(d));updatePrompt(d);setMessage("Repository default restored");
  };
  const loadPromptHistory=async()=>setPromptHistory(await fetch(api+"/settings/prompts/"+promptKey+"/history").then(r=>r.json()));
  const rollbackPrompt=async revision=>{
    const r=await fetch(api+"/settings/prompts/"+promptKey+"/rollback",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({revision})});
    const d=await r.json();if(!r.ok)throw new Error(d.detail||JSON.stringify(d));updatePrompt(d);await loadPromptHistory();setMessage("Rolled back as new revision "+d.revision);
  };
  const testPrompt=async()=>{
    const variables=Object.fromEntries((activePrompt.required_variables||[]).map(k=>[k,
      k==="cv_sections"?' {"about":"Experienced candidate"} ':k.includes("text")||k==="jd_text"||k==="cv_text"?"Sample content for prompt test.":"Sample"]));
    setMessage("Testing prompt without cache…");
    const r=await fetch(api+"/settings/prompts/"+promptKey+"/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({variables})});
    const d=await r.json();setMessage(r.ok?`✓ ${d.model} · ${d.latency_ms}ms · ${d.prompt_tokens||"?"}+${d.completion_tokens||"?"} tokens · ${String(d.response).slice(0,180)}`:"✗ "+(d.detail||JSON.stringify(d)));
  };
  const saveAutomation=async()=>{
    const r=await fetch(api+"/settings/automation",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(automation)});
    const d=await r.json();if(!r.ok)throw new Error(d.detail||JSON.stringify(d));setAutomation(d);setMessage("Automation saved");
  };
  const runNow=async()=>{
    const r=await fetch(api+"/automation/run-now",{method:"POST"});const d=await r.json();
    setMessage(r.ok?"Pipeline started · run #"+d.run_id:"✗ "+(d.detail||JSON.stringify(d)));
    setTimeout(()=>load(),1500);
  };
  const cancelRun=async id=>{
    const r=await fetch(api+`/automation/runs/${id}/cancel`,{method:"POST"});const d=await r.json();
    setMessage(r.ok?`Run #${id} is cancelling`:"✗ "+(d.detail||JSON.stringify(d)));setTimeout(()=>load(),1000);
  };

  if(!llm||!priority||!automation)return <div style={{padding:30,color:"#5a7a68"}}>Loading settings…</div>;
  return <div style={{flex:1,overflowY:"auto",padding:20,display:"grid",gridTemplateColumns:"minmax(360px,1fr) minmax(440px,1.35fr)",gap:16,alignItems:"start"}}>
    <section style={box}>
      <div style={{fontWeight:800,marginBottom:12}}>ROLES & CV</div>
      <div style={{display:"flex",gap:5,flexWrap:"wrap",marginBottom:12}}>
        {profiles.map(p=><button key={p.id} style={{...button,background:selected?.id===p.id?"#2e7d5225":"transparent"}} onClick={()=>{setSelected(p);setPreview(null);}}>{p.name}</button>)}
        <button style={button} onClick={newProfile}>+ NEW</button>
      </div>
      {selected&&<div style={{display:"flex",flexDirection:"column",gap:9}}>
        <label>Name<input style={input} value={selected.name} onChange={e=>setSelected({...selected,name:e.target.value})}/></label>
        <label>Slug<input style={input} value={selected.slug} onChange={e=>setSelected({...selected,slug:e.target.value})}/></label>
        <label>Search tags<TagEditor value={selected.keywords||[]} onChange={keywords=>setSelected({...selected,keywords})}/></label>
        <label>Default city
          <div style={{display:"flex",gap:5}}><input style={input} value={selected.default_location||""} placeholder="Blank = all Switzerland" onChange={e=>setSelected({...selected,default_location:e.target.value})}/><button style={button} onClick={()=>setSelected({...selected,default_location:""})}>ALL CH</button></div>
        </label>
        <label style={{...button,textAlign:"center"}}>
          {parsing?"PARSING…":"UPLOAD PDF / DOCX / TXT"}
          <input type="file" accept=".pdf,.docx,.txt" disabled={parsing} onChange={e=>parseFile(e.target.files?.[0]).catch(err=>setMessage(err.message))} style={{display:"none"}}/>
        </label>
        {preview&&<div style={{...box,background:"#fff"}}>
          <b>IMPORT PREVIEW · {preview.detected_format.toUpperCase()}</b>
          {preview.warnings?.map(w=><div key={w} style={{fontSize:9,color:"#f59e0b"}}>{w}</div>)}
          <div style={{display:"flex",gap:6,marginTop:8}}>
            <button style={button} onClick={()=>{setSelected({...selected,cv_sections:preview.cv_sections});setPreview(null);setMessage("Preview applied; save the profile to persist it");}}>APPLY TO PROFILE</button>
            <button style={button} onClick={()=>setPreview(null)}>CANCEL</button>
          </div>
        </div>}
        <div>
          <div style={{fontWeight:700,fontSize:10,marginBottom:5}}>CV SECTIONS</div>
          {SECTIONS.map(([key,label])=><div key={key} style={{borderBottom:"1px solid #d4dece"}}>
            <button style={{...button,width:"100%",textAlign:"left",border:0,background:"transparent"}} onClick={()=>setExpanded(expanded===key?"":key)}>
              {expanded===key?"▾":"▸"} {label}{selected.cv_sections?.[key]?.trim()?" ✓":""}
            </button>
            {expanded===key&&<textarea style={{...input,minHeight:key==="experience"?220:120,marginBottom:7}} value={selected.cv_sections?.[key]||""} onChange={e=>setSelected({...selected,cv_sections:{...emptySections(),...selected.cv_sections,[key]:e.target.value}})}/>}
          </div>)}
        </div>
        <label><input type="checkbox" checked={selected.active} onChange={e=>setSelected({...selected,active:e.target.checked})}/> Active</label>
        <div style={{display:"flex",gap:8}}><button style={button} onClick={()=>saveProfile().catch(e=>setMessage(e.message))}>SAVE PROFILE</button><button style={{...button,color:"#c0392b"}} onClick={deleteProfile}>DELETE</button></div>
      </div>}
    </section>

    <div style={{display:"flex",flexDirection:"column",gap:16}}>
      <div style={{display:"flex",gap:6}}>{[["ai","AI ROUTING"],["prompts","PROMPTS"],["automation","AUTOMATION"],["priority","PRIORITY"]].map(([id,label])=><button key={id} style={{...button,background:panel===id?"#2e7d5225":"transparent"}} onClick={()=>setPanel(id)}>{label}</button>)}</div>
      {panel==="ai"&&<section style={box}>
        <div style={{fontWeight:800}}>OPENROUTER ROUTING</div>
        <div style={{fontSize:10,color:llm.openrouter_configured?"#2e7d52":"#c0392b",marginBottom:10}}>API key: {llm.openrouter_configured?"configured":"missing in environment"}</div>
        <label>Default model<input style={input} value={llm.default_model} onChange={e=>setLlm({...llm,default_model:e.target.value})}/></label>
        <label>Global fallbacks<textarea style={{...input,minHeight:55}} value={(llm.global_fallbacks||[]).join("\n")} onChange={e=>setLlm({...llm,global_fallbacks:e.target.value.split("\n").map(x=>x.trim()).filter(Boolean)})}/></label>
        <div style={{display:"grid",gridTemplateColumns:"1fr 1.2fr 1.2fr auto",gap:6,alignItems:"center",marginTop:10}}>
          <b>Operation</b><b>Model override</b><b>Fallbacks</b><span/>
          {OPERATIONS.map(op=><div key={op} style={{display:"contents"}}>
            <span style={{fontSize:10}}>{op}</span>
            <input style={input} placeholder="use default" value={llm.operations?.[op]?.model||""} onChange={e=>setLlm({...llm,operations:{...llm.operations,[op]:{...llm.operations[op],model:e.target.value}}})}/>
            <input style={input} placeholder="comma-separated / global" value={(llm.operations?.[op]?.fallbacks||[]).join(", ")} onChange={e=>setLlm({...llm,operations:{...llm.operations,[op]:{...llm.operations[op],fallbacks:e.target.value.split(",").map(x=>x.trim()).filter(Boolean)}}})}/>
            <button style={button} onClick={()=>testModel(op)}>TEST</button>
          </div>)}
        </div>
        <div style={{display:"flex",gap:8,marginTop:12}}>{["timeout","retries","concurrency"].map(k=><label key={k} style={{fontSize:10}}>{k}<input type="number" style={{...input,width:90}} value={llm[k]} onChange={e=>setLlm({...llm,[k]:Number(e.target.value)})}/></label>)}</div>
        <button style={{...button,marginTop:12}} onClick={()=>saveLlm().catch(e=>setMessage(e.message))}>SAVE AI ROUTING</button>
      </section>}
      {panel==="priority"&&<section style={box}>
        <div style={{fontWeight:800,marginBottom:10}}>SEARCH PRIORITY</div>
        <div style={{display:"flex",gap:10}}>
          <label>Match weight<input type="number" step=".05" min="0" max="1" style={input} value={priority.match_weight} onChange={e=>setPriority({...priority,match_weight:Number(e.target.value)})}/></label>
          <label>Freshness weight<input type="number" step=".05" min="0" max="1" style={input} value={priority.freshness_weight} onChange={e=>setPriority({...priority,freshness_weight:Number(e.target.value)})}/></label>
        </div>
        <div style={{fontSize:10,color:"#5a7a68",margin:"8px 0"}}>Freshness: {priority.buckets.map(b=>(b.max_hours/24)+"d="+b.score).join(" · ")}</div>
        <button style={button} onClick={()=>savePriority().catch(e=>setMessage(e.message))}>SAVE PRIORITY</button>
      </section>}
      {panel==="prompts"&&activePrompt&&<section style={box}>
        <div style={{fontWeight:800,marginBottom:10}}>PROMPT MANAGEMENT</div>
        <div style={{display:"flex",gap:5,flexWrap:"wrap",marginBottom:12}}>{prompts.map(p=><button key={p.key} style={{...button,background:p.key===promptKey?"#2e7d5225":"transparent"}} onClick={()=>{setPromptKey(p.key);setPromptHistory([]);}}>{p.key}</button>)}</div>
        <div style={{fontSize:10,color:"#5a7a68",marginBottom:8}}>Operation: {activePrompt.operation} · active revision {activePrompt.revision} · variables: {(activePrompt.required_variables||[]).map(v=>`{${v}}`).join(" ")}</div>
        <label>System template<textarea style={{...input,minHeight:120}} value={activePrompt.system_template} onChange={e=>updatePrompt({system_template:e.target.value})}/></label>
        <label>User template<textarea style={{...input,minHeight:300}} value={activePrompt.user_template} onChange={e=>updatePrompt({user_template:e.target.value})}/></label>
        <div style={{display:"flex",gap:8,marginTop:8}}><label>Temperature<input type="number" min="0" max="2" step=".1" style={{...input,width:100}} value={activePrompt.temperature} onChange={e=>updatePrompt({temperature:Number(e.target.value)})}/></label><label>Max tokens<input type="number" style={{...input,width:120}} value={activePrompt.max_tokens} onChange={e=>updatePrompt({max_tokens:Number(e.target.value)})}/></label></div>
        <div style={{display:"flex",gap:7,marginTop:12}}><button style={button} onClick={()=>savePrompt().catch(e=>setMessage(e.message))}>SAVE & ACTIVATE</button><button style={button} onClick={()=>testPrompt().catch(e=>setMessage(e.message))}>TEST</button><button style={button} onClick={()=>resetPrompt().catch(e=>setMessage(e.message))}>RESTORE DEFAULT</button><button style={button} onClick={()=>loadPromptHistory().catch(e=>setMessage(e.message))}>HISTORY</button></div>
        {promptHistory.length>0&&<div style={{marginTop:12}}>{promptHistory.map(h=><details key={h.id} style={{fontSize:10,borderTop:"1px solid #d4dece",padding:"6px 0"}}><summary style={{display:"flex",justifyContent:"space-between",cursor:"pointer"}}><span>revision {h.revision} · {h.created_at}{h.active?" · ACTIVE":""}</span>{!h.active&&<button style={button} onClick={e=>{e.preventDefault();rollbackPrompt(h.revision).catch(err=>setMessage(err.message));}}>ROLL BACK</button>}</summary>{h.diff_to_active&&<pre style={{whiteSpace:"pre-wrap",maxHeight:220,overflow:"auto",background:"#fff",padding:8}}>{h.diff_to_active}</pre>}</details>)}</div>}
      </section>}
      {panel==="automation"&&<section style={box}>
        <div style={{fontWeight:800,marginBottom:10}}>AUTOMATED PIPELINE</div>
        <label><input type="checkbox" checked={automation.enabled} onChange={e=>setAutomation({...automation,enabled:e.target.checked})}/> Enabled · weekdays 07:00–22:00 Europe/Zurich</label>
        <div style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:8,marginTop:10}}>
          <label>Interval, min<input type="number" style={input} value={automation.interval_minutes} onChange={e=>setAutomation({...automation,interval_minutes:Number(e.target.value)})}/></label>
          <label>Pages/source<input type="number" style={input} value={automation.pages_per_source} onChange={e=>setAutomation({...automation,pages_per_source:Number(e.target.value)})}/></label>
          <label>Max age, days<input type="number" style={input} value={automation.max_age_days} onChange={e=>setAutomation({...automation,max_age_days:Number(e.target.value)})}/></label>
          <label>Enrich/run<input type="number" style={input} value={automation.enrich_limit} onChange={e=>setAutomation({...automation,enrich_limit:Number(e.target.value)})}/></label>
          <label>Date backfill/run<input type="number" style={input} value={automation.date_backfill_limit} onChange={e=>setAutomation({...automation,date_backfill_limit:Number(e.target.value)})}/></label>
          <label>Screen/profile<input type="number" style={input} value={automation.screen_limit_per_profile} onChange={e=>setAutomation({...automation,screen_limit_per_profile:Number(e.target.value)})}/></label>
          <label>Companies/run<input type="number" style={input} value={automation.company_limit} onChange={e=>setAutomation({...automation,company_limit:Number(e.target.value)})}/></label>
          <label>Availability/run<input type="number" style={input} value={automation.availability_limit} onChange={e=>setAutomation({...automation,availability_limit:Number(e.target.value)})}/></label>
          <label>Availability hours<input type="number" style={input} value={automation.availability_interval_hours} onChange={e=>setAutomation({...automation,availability_interval_hours:Number(e.target.value)})}/></label>
          <label>Concurrency<input type="number" style={input} value={automation.concurrency} onChange={e=>setAutomation({...automation,concurrency:Number(e.target.value)})}/></label>
        </div>
        <label>Sources<TagEditor value={automation.sources||[]} onChange={sources=>setAutomation({...automation,sources})}/></label>
        <div style={{display:"flex",gap:10,flexWrap:"wrap",marginTop:8}}>{[["enrich","Enrich"],["screen","Screen"],["company_enrichment","Companies"],["include_unknown_dates","Keep unknown dates"]].map(([key,label])=><label key={key}><input type="checkbox" checked={!!automation[key]} onChange={e=>setAutomation({...automation,[key]:e.target.checked})}/> {label}</label>)}</div>
        <div style={{display:"flex",gap:7,marginTop:12}}><button style={button} onClick={()=>saveAutomation().catch(e=>setMessage(e.message))}>SAVE AUTOMATION</button><button style={button} onClick={runNow}>RUN NOW</button></div>
        <div style={{fontWeight:700,fontSize:10,marginTop:16}}>RECENT RUNS</div>
        {runs.slice(0,8).map(r=><div key={r.id} style={{fontSize:10,borderTop:"1px solid #d4dece",padding:"6px 0",display:"flex",gap:6,alignItems:"center"}}><span>#{r.id} · {r.status} · {r.started_at} · +{r.stats?.new||0} jobs · {r.stats?.phase_seconds?.total?Math.round(r.stats.phase_seconds.total)+"s · ":""}dates {Object.entries(r.stats?.date_coverage||{}).map(([s,v])=>s+":"+v.percent+"%").join(" ")}</span>{r.status==="running"&&<button style={button} onClick={()=>cancelRun(r.id)}>CANCEL</button>}</div>)}
      </section>}
      {message&&<div style={{...box,color:"#2e7d52",fontSize:11}}>{message}</div>}
    </div>
  </div>;
}
