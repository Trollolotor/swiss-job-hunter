import { useEffect, useState } from "react";

export default function MatchingSettingsView({api}) {
  const [settings,setSettings]=useState(null);
  const [prices,setPrices]=useState("{}");
  const [spent,setSpent]=useState(0);
  const [message,setMessage]=useState("");
  useEffect(()=>{fetch(api+"/settings/matching").then(r=>r.json()).then(d=>{
    setSettings(d.settings);setPrices(JSON.stringify(d.settings.prices,null,2));setSpent(d.daily_cost_upper_bound_usd);
  }).catch(e=>setMessage(e.message));},[api]);
  const save=async e=>{
    e.preventDefault();
    try{
      const r=await fetch(api+"/settings/matching",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({...settings,prices:JSON.parse(prices)})});
      const d=await r.json();if(!r.ok)throw new Error(typeof d.detail==="string"?d.detail:JSON.stringify(d.detail));
      setSettings(d.settings);setMessage("Matching policy saved");
    }catch(error){setMessage(error.message);}
  };
  return <section style={{padding:16,border:"1px solid #d4dece",borderRadius:6}}>
    <h3>CV matching policy</h3>
    <p>Keyword matching is local and provisional. Paid screening uses its own daily budget, separate from employer onboarding and repair.</p>
    {settings&&<form onSubmit={save} style={{display:"grid",gap:10,maxWidth:600}}>
      {[["shortlist_threshold","Shortlist threshold",0,1,.05],["archive_threshold","Automatic archive threshold",0,1,.05],["concurrency","Matching concurrency",1,20,1],["max_attempt_usd","Maximum cost per attempt (USD)",.001,100,"any"]].map(([key,label,min,max,step])=><label key={key}>{label} <input aria-label={label} type="number" min={min} max={max} step={step} value={settings[key]} onChange={e=>setSettings({...settings,[key]:Number(e.target.value)})}/></label>)}
      <label>Daily matching cap (USD) <input aria-label="Daily matching cap (USD)" type="number" min="0.001" max="1000" step="any" value={settings.daily_budget_usd??""} placeholder="Unset: paid screening waits" onChange={e=>setSettings({...settings,daily_budget_usd:e.target.value===""?null:Number(e.target.value)})}/></label>
      <label>Model prices (USD per million tokens)<textarea aria-label="Matching model prices" rows={7} value={prices} onChange={e=>setPrices(e.target.value)}/></label>
      <small>Use model slugs for OpenRouter, or provider:model for legacy providers. Fields: input_per_million and output_per_million. Unknown prices block paid calls.</small>
      <p>Today’s cost and outstanding reservations: ${Number(spent).toFixed(4)}. Resets at midnight UTC.</p>
      <button type="submit">Save matching policy</button>
    </form>}
    {message&&<p role="status">{message}</p>}
  </section>;
}
