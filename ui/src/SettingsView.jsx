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

  const load=async()=>{
    const [p,l,pr]=await Promise.all([
      fetch(api+"/profiles").then(r=>r.json()),
      fetch(api+"/settings/llm").then(r=>r.json()),
      fetch(api+"/settings/search-priority").then(r=>r.json()),
    ]);
    setProfiles(p);setSelected(old=>p.find(x=>x.id===old?.id)||p[0]||null);
    setLlm(l);setPriority(pr);
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

  if(!llm||!priority)return <div style={{padding:30,color:"#5a7a68"}}>Loading settings…</div>;
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
      <section style={box}>
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
      </section>
      <section style={box}>
        <div style={{fontWeight:800,marginBottom:10}}>SEARCH PRIORITY</div>
        <div style={{display:"flex",gap:10}}>
          <label>Match weight<input type="number" step=".05" min="0" max="1" style={input} value={priority.match_weight} onChange={e=>setPriority({...priority,match_weight:Number(e.target.value)})}/></label>
          <label>Freshness weight<input type="number" step=".05" min="0" max="1" style={input} value={priority.freshness_weight} onChange={e=>setPriority({...priority,freshness_weight:Number(e.target.value)})}/></label>
        </div>
        <div style={{fontSize:10,color:"#5a7a68",margin:"8px 0"}}>Freshness: {priority.buckets.map(b=>(b.max_hours/24)+"d="+b.score).join(" · ")}</div>
        <button style={button} onClick={()=>savePriority().catch(e=>setMessage(e.message))}>SAVE PRIORITY</button>
      </section>
      {message&&<div style={{...box,color:"#2e7d52",fontSize:11}}>{message}</div>}
    </div>
  </div>;
}
