import { useState } from "react";

export function splitTags(values) {
  const source = Array.isArray(values) ? values : [values || ""];
  const seen = new Set();
  const result = [];
  source.forEach(value => String(value).split(/[,;\n]+/).forEach(part => {
    const tag = part.replace(/\s+/g, " ").trim();
    const key = tag.toLocaleLowerCase();
    if (tag && !seen.has(key)) { seen.add(key); result.push(tag); }
  }));
  return result;
}

export default function TagEditor({value=[], onChange, placeholder="Add tag"}) {
  const [draft,setDraft]=useState("");
  const tags=splitTags(value);
  const commit=(text)=>{
    const additions=splitTags(text);
    if(additions.length) onChange(splitTags([...tags,...additions]));
    setDraft("");
  };
  return <div>
    <div style={{display:"flex",flexWrap:"wrap",gap:4,marginBottom:5}}>
      {tags.map(tag=><span key={tag.toLocaleLowerCase()} style={{display:"inline-flex",gap:5,alignItems:"center",padding:"3px 7px",borderRadius:12,background:"#2e7d5218",border:"1px solid #2e7d5235",fontSize:9,color:"#2e7d52"}}>
        {tag}<button type="button" onClick={()=>onChange(tags.filter(x=>x!==tag))} style={{border:0,background:"none",color:"inherit",cursor:"pointer",padding:0}}>×</button>
      </span>)}
    </div>
    <input value={draft} placeholder={placeholder} onChange={e=>{
      const next=e.target.value;
      if(/[,;\n]$/.test(next)) commit(next); else setDraft(next);
    }} onPaste={e=>{
      const text=e.clipboardData.getData("text");
      if(/[,;\n]/.test(text)){e.preventDefault();commit(text);}
    }} onKeyDown={e=>{
      if(e.key==="Enter"){e.preventDefault();commit(draft);}
      if(e.key==="Backspace"&&!draft&&tags.length) onChange(tags.slice(0,-1));
    }} onBlur={()=>draft.trim()&&commit(draft)}
      style={{width:"100%",padding:"7px 9px",border:"1px solid #c8d8c4",borderRadius:4,fontFamily:"monospace",fontSize:11}}/>
  </div>;
}
