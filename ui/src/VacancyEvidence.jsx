export default function VacancyEvidence({job}) {
  const date=value=>value?new Date(value.endsWith("Z")?value:value+"Z").toLocaleString():"unknown";
  return <section aria-label="Vacancy evidence" style={{fontSize:11,lineHeight:1.7,marginBottom:12}}>
    <div><b>Fit:</b> {job.match_score==null?"Not evaluated":`${Math.round(job.match_score*100)}% (${job.score_method||"legacy"})`}
      {job.matching_profile&&` · ${job.matching_profile.name}`}</div>
    <div><b>Evaluation:</b> {job.evaluation_state||"pending"}{job.score_stale?" · previous score needs refresh":""}</div>
    <div><b>Extraction:</b> {job.description_quality||"unknown"}{job.extraction?.reason&&` · ${job.extraction.reason}`}</div>
    {job.availability&&<div><b>Availability:</b> {job.availability}</div>}
    <div><b>Freshness:</b> published {job.posted_at?date(job.posted_at):"unknown"} · first stored {date(job.scraped_at)}</div>
    {job.keyword_score!=null&&<div>Provisional CV keyword coverage: {Math.round(job.keyword_score*100)}%. Requirements remain unconfirmed until screened.</div>}
    {job.direct_only&&<div>Not yet observed on our monitored boards.</div>}
    {(job.sources||[]).map(source=><div key={source.id}>
      <a href={source.url} target="_blank" rel="noreferrer">{source.direct?"Employer":"Board"}: {source.namespace}</a>
      {` · ${source.availability} · first ${date(source.first_seen_at)} · last ${date(source.last_seen_at)}`}
    </div>)}
    {!!job.requirements?.length&&<details><summary>Requirement evidence</summary><ul>{job.requirements.map((r,i)=><li key={i}>
      <b>{r.requirement}</b> · {r.importance} · {r.state}
      {r.jd_evidence&&<div>Vacancy: {r.jd_evidence}</div>}{r.cv_evidence&&<div>CV: {r.cv_evidence}</div>}
    </li>)}</ul></details>}
  </section>;
}
