import { useState } from "react";
import VacancyEvidence from "./VacancyEvidence";

export default function ReviewQueue({jobs,onDecision,onPrepare,onSelect}) {
  const [reasons,setReasons]=useState({});
  const [error,setError]=useState("");
  const [busy,setBusy]=useState(null);
  const decide=async(job,status)=>{setBusy(job.id);setError("");try{await onDecision(job.id,status,reasons[job.id]||"");}catch(e){setError(e.message);}finally{setBusy(null);}};
  return <section aria-label="Shared review queue" style={{flex:1,overflowY:"auto",padding:20}}>
    <h2>Review queue</h2><p>Review decisions and application history are shared across CV profiles.</p>
    {error&&<p role="alert">{error}</p>}
    {!jobs.length&&<p>No vacancies in the current review filters.</p>}
    {jobs.map(job=><article key={job.id} style={{padding:16,marginBottom:12,border:"1px solid #d4dece",borderRadius:6}}>
      <h3>{job.title} · {job.company}</h3><VacancyEvidence job={job}/>
      <label>Optional skip reason <input aria-label={`Skip reason for ${job.title}`} value={reasons[job.id]||""} onChange={e=>setReasons({...reasons,[job.id]:e.target.value})}/></label>
      <div style={{display:"flex",gap:10,marginTop:10}}>
        <button disabled={busy===job.id} onClick={()=>decide(job,"considering")}>Consider</button>
        <button disabled={busy===job.id} onClick={()=>decide(job,"archived")}>Skip</button>
        <button onClick={()=>onPrepare(job)} disabled={job.description_quality!=="complete"}>Prepare CV</button>
        <button onClick={()=>onSelect(job)}>Read vacancy</button>
        <a href={job.application_url||job.url} target="_blank" rel="noreferrer">Open application</a>
      </div>
    </article>)}
  </section>;
}
