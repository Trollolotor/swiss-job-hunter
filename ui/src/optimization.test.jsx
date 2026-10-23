import test,{afterEach} from "node:test";
import assert from "node:assert/strict";
import {JSDOM} from "jsdom";
import React from "react";

const dom=new JSDOM("<!doctype html><html><body></body></html>",{url:"http://test/"});
globalThis.window=dom.window;globalThis.document=dom.window.document;
Object.defineProperty(globalThis,"navigator",{value:dom.window.navigator,configurable:true});
globalThis.HTMLElement=dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const {render,screen,cleanup,waitFor,fireEvent,within,act}=await import("@testing-library/react");
const {default:userEvent}=await import("@testing-library/user-event");
const {default:MatchingSettingsView}=await import("./MatchingSettingsView");
const {default:ReviewQueue}=await import("./ReviewQueue");
const {default:VacancyEvidence}=await import("./VacancyEvidence");
const {vacancyQuery}=await import("./vacancyQuery");
const {default:App,TrackerBoard}=await import("./App");
const {default:DetailPane}=await import("./DetailPane");
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;window.localStorage.clear();window.innerWidth=1024;});
const ok=value=>({ok:true,json:async()=>value});
const settings={shortlist_threshold:.3,archive_threshold:.1,concurrency:5,daily_budget_usd:null,max_attempt_usd:1,prices:{}};

test("matching cap is unset until explicitly saved, with independent thresholds",async()=>{
  const requests=[];
  globalThis.fetch=async(url,options)=>{
    requests.push({url,options});
    return ok({settings:options?JSON.parse(options.body):settings,daily_cost_upper_bound_usd:0});
  };
  render(<MatchingSettingsView api="http://test"/>);
  const user=userEvent.setup();
  const cap=await screen.findByLabelText("Daily matching cap (USD)");
  assert.equal(cap.value,"");
  await user.type(cap,"2");
  const threshold=screen.getByLabelText("Shortlist threshold");
  await user.clear(threshold);await user.type(threshold,"0.7");
  await user.click(screen.getByRole("button",{name:"Save matching policy"}));
  await screen.findByText("Matching policy saved");
  const saved=JSON.parse(requests.find(r=>r.options?.method==="PUT").options.body);
  assert.equal(saved.daily_budget_usd,2);assert.equal(saved.shortlist_threshold,.7);assert.equal(saved.archive_threshold,.1);
});

test("review skip retains its explicit reason and the queue has keyboard controls",async()=>{
  const decisions=[];
  const job={id:1,title:"Engineer",company:"Acme",description_quality:"complete",url:"https://example.test/job"};
  render(<ReviewQueue jobs={[job]} onDecision={async(...args)=>decisions.push(args)} onPrepare={()=>{}} onSelect={()=>{}}/>);
  const user=userEvent.setup();
  await user.type(screen.getByLabelText("Skip reason for Engineer"),"Different industry");
  screen.getByRole("button",{name:"Skip"}).focus();
  await user.keyboard("{Enter}");
  assert.deepEqual(decisions,[[1,"archived","Different industry"]]);
  assert.equal(screen.getByRole("link",{name:"Open application"}).href,job.url);
});

test("fit, freshness and extraction are shown separately with source evidence",()=>{
  render(<VacancyEvidence job={{description_quality:"preview",evaluation_state:"deferred",direct_only:true,
    keyword_score:.4,sources:[{id:1,direct:true,namespace:"ats:tenant",url:"https://example.test/role",availability:"open"}]}}/>);
  assert.ok(screen.getByText("Not evaluated"));assert.ok(screen.getByText(/preview/));
  assert.ok(screen.getByText(/Not yet observed on our monitored boards/));
  assert.ok(screen.getByText(/Requirements remain unconfirmed/));
});

test("all-profile queries omit empty profile identifiers",()=>{
  const params=vacancyQuery({direction:"all",directOnly:true,reviewQueue:true,minScore:.5});
  assert.equal(params.has("profile_id"),false);assert.equal(params.get("direct_only"),"true");
  assert.equal(params.get("review_queue"),"true");assert.equal(params.get("min_score"),"0.5");
});

test("source and active filters reach both list and statistics without changing processing",async()=>{
  const requests=[];
  globalThis.fetch=async(url,options)=>{
    requests.push({url:String(url),options});
    const path=new URL(String(url)).pathname;
    if(path==="/profiles")return ok([]);
    if(path==="/jobs")return ok([]);
    if(path==="/stats")return ok({total:0});
    if(path==="/presets"||path==="/config")return ok({});
    return ok({});
  };
  render(<App/>);
  const user=userEvent.setup();
  assert.equal(screen.getByRole("button",{name:"Filter status active"}).getAttribute("aria-pressed"),"true");
  await user.selectOptions(screen.getByLabelText("Filter by source"),"employer");
  await waitFor(()=>{
    for(const path of ["/jobs","/stats"]){
      assert.ok(requests.some(r=>{
        const url=new URL(r.url);
        return url.pathname===path&&url.searchParams.get("source")==="employer"&&url.searchParams.get("status")==="active";
      }));
    }
  });
  await user.click(screen.getByRole("button",{name:"Filter status archived"}));
  await waitFor(()=>assert.ok(requests.some(r=>new URL(r.url).searchParams.get("status")==="archived")));
  assert.ok(requests.every(r=>!r.options?.method||r.options.method==="GET"));
  assert.equal(screen.getByLabelText("Purge maximum fit (%)").value,"10");
});

test("tracker drag is optimistic, blocks duplicate writes, and restores a failed move",async()=>{
  const job={id:1,title:"Python role",company:"Acme",status:"considering"};
  let resolvePatch;
  const patches=[],failures=[];
  globalThis.fetch=async(url,options)=>{
    if(options?.method==="PATCH"){
      patches.push(JSON.parse(options.body));
      return new Promise(resolve=>{resolvePatch=resolve;});
    }
    return ok([job]);
  };
  render(<TrackerBoard onChangeFailed={(...args)=>failures.push(args)}/>);
  const card=(await screen.findByText(job.title)).closest("[draggable]");
  const data=new Map();
  const dataTransfer={setData:(key,value)=>data.set(key,value),getData:key=>data.get(key)};
  fireEvent.dragStart(card,{dataTransfer});
  fireEvent.drop(screen.getByRole("region",{name:"Tracker applied"}),{dataTransfer});
  assert.ok(within(screen.getByRole("region",{name:"Tracker applied"})).getByText(job.title));
  fireEvent.drop(screen.getByRole("region",{name:"Tracker offer"}),{dataTransfer});
  assert.equal(patches.length,1);
  await act(async()=>resolvePatch({ok:false,status:500}));
  assert.ok(await screen.findByRole("alert"));
  assert.ok(within(screen.getByRole("region",{name:"Tracker considering"})).getByText(job.title));
  assert.deepEqual(failures,[[1,"applied"]]);
});

test("tracker keyboard moves refresh confirmed state and preserve employer links",async()=>{
  const job={id:1,title:"Employer role",company:"Acme",status:"considering",
    application_url:"https://careers.example.test/apply",sources:[{direct:true}],notes:"Referral"};
  const changed=[];
  globalThis.fetch=async(url,options)=>{
    if(options?.method==="PATCH"){
      job.status=JSON.parse(options.body).status;
      job.applied_at="2026-01-02T10:30:00";
      return ok({ok:true});
    }
    if(String(url).endsWith("/events"))return ok([]);
    return ok([{...job}]);
  };
  render(<TrackerBoard onChanged={(...args)=>changed.push(args)}/>);
  const move=await screen.findByLabelText("Move Employer role");
  await userEvent.setup().selectOptions(move,"applied");
  await waitFor(()=>assert.deepEqual(changed,[[1,"applied"]]));
  await waitFor(()=>assert.equal(screen.getByLabelText("Move Employer role").value,"applied"));
  assert.ok(within(screen.getByRole("region",{name:"Tracker applied"})).getByText(job.title));
  assert.equal(screen.queryByRole("region",{name:"Tracker viewed"}),null);
  assert.equal(job.application_url,"https://careers.example.test/apply");
});

test("a tracker refresh failure after saving never rolls back the saved move",async()=>{
  let saved=false;
  globalThis.fetch=async(url,options)=>{
    if(options?.method==="PATCH"){saved=true;return ok({ok:true});}
    if(saved)throw new Error("offline");
    return ok([{id:1,title:"Saved role",company:"Acme",status:"considering"}]);
  };
  render(<TrackerBoard/>);
  await userEvent.setup().selectOptions(await screen.findByLabelText("Move Saved role"),"archived");
  assert.match((await screen.findByRole("alert")).textContent,/Status saved/);
  assert.ok(within(screen.getByRole("region",{name:"Tracker archived"})).getByText("Saved role"));
});

test("status controls stay visible across tabs and tracker moves refresh the selected vacancy",async()=>{
  const job={id:1,title:"Workflow role",company:"Acme",status:"considering",description:"Preserved employer description",
    description_quality:"complete",match_score:.8,source:"jobs.ch",profiles:[],
    application_url:"https://careers.example.test/apply/1"};
  const requests=[];
  globalThis.fetch=async(url,options)=>{
    const parsed=new URL(String(url));
    requests.push({url:parsed,options});
    if(options?.method==="PATCH"){job.status=JSON.parse(options.body).status;return ok({ok:true});}
    if(parsed.pathname==="/jobs")return ok(job.status==="archived"?[]:[{...job}]);
    if(parsed.pathname==="/tracker")return ok([{...job}]);
    if(parsed.pathname==="/stats")return ok({total:job.status==="archived"?0:1,by_status:{}});
    if(parsed.pathname==="/profiles"||parsed.pathname.endsWith("/events"))return ok([]);
    if(parsed.pathname.startsWith("/companies/"))return ok({summary:"Cached company info"});
    return ok({});
  };
  render(<App/>);
  const user=userEvent.setup();
  await user.click(await screen.findByText(job.title));
  await user.click(screen.getByRole("button",{name:"COMPANY",exact:true}));
  const controls=screen.getByRole("group",{name:"Update vacancy status"});
  assert.ok(within(controls).getByRole("button",{name:"Set status archived"}));
  const beforeStats=requests.filter(r=>r.url.pathname==="/stats").length;
  await user.click(screen.getByRole("button",{name:"TRACKER",exact:true}));
  await user.selectOptions(await screen.findByLabelText("Move Workflow role"),"archived");
  await waitFor(()=>assert.ok(requests.filter(r=>r.url.pathname==="/stats").length>beforeStats));
  await user.click(screen.getByRole("button",{name:"BOARD",exact:true}));
  assert.equal(screen.getByRole("button",{name:"Set status archived"}).getAttribute("aria-pressed"),"true");
  await user.click(screen.getByRole("button",{name:"DETAIL",exact:true}));
  assert.ok(screen.getByText(job.description));
  assert.equal(screen.getByRole("link",{name:/EMPLOYER APPLICATION PAGE/}).href,job.application_url);
  assert.equal(requests.some(r=>r.url.pathname.startsWith("/run/")),false);
});

test("detail width supports keyboard and pointer resizing, persistence, and narrow windows",async()=>{
  window.innerWidth=1400;
  window.localStorage.setItem("sjh.detailWidth","500");
  const view=render(<div><div>List</div><DetailPane>Details</DetailPane></div>);
  const divider=screen.getByRole("separator",{name:"Resize vacancy details"});
  assert.equal(divider.getAttribute("aria-valuenow"),"500");
  divider.focus();
  await userEvent.setup().keyboard("{ArrowLeft}");
  assert.equal(divider.getAttribute("aria-valuenow"),"520");
  assert.equal(window.localStorage.getItem("sjh.detailWidth"),"520");
  const pane=screen.getByLabelText("Vacancy details");
  pane.getBoundingClientRect=()=>({right:1400});
  fireEvent(divider,new window.MouseEvent("pointerdown",{bubbles:true,button:0,clientX:880}));
  fireEvent(window,new window.MouseEvent("pointermove",{clientX:800}));
  fireEvent(window,new window.MouseEvent("pointerup"));
  assert.equal(divider.getAttribute("aria-valuenow"),"600");
  window.innerWidth=800;fireEvent(window,new window.Event("resize"));
  assert.equal(divider.getAttribute("aria-valuenow"),"250");
  view.unmount();
  window.innerWidth=1400;
  render(<div><div>List</div><DetailPane>Details</DetailPane></div>);
  assert.equal(screen.getByRole("separator").getAttribute("aria-valuenow"),"600");
});

test("detail pane tolerates unavailable browser storage",()=>{
  const storage=Object.getOwnPropertyDescriptor(window,"localStorage");
  try{
    Object.defineProperty(window,"localStorage",{configurable:true,get(){throw new Error("private mode");}});
    render(<div><div>List</div><DetailPane>Details</DetailPane></div>);
    assert.ok(screen.getByRole("separator"));
  }finally{Object.defineProperty(window,"localStorage",storage);}
});

test("changing a display filter never changes scoring or purge parameters",async()=>{
  const requests=[];
  globalThis.fetch=async(url,options)=>{
    requests.push({url:String(url),options});
    const path=new URL(String(url)).pathname;
    if(path==="/profiles")return ok([{id:1,slug:"python",name:"Python",active:true,keywords:["Python"],default_location:"Bern"}]);
    if(path==="/jobs")return ok([{id:1,title:"Engineer",company:"Acme",status:"new",match_score:.8,profiles:[],description:"Full description",source:"jobs.ch"}]);
    if(path==="/stats")return ok({total:1,by_status:{},above_threshold:1});
    if(path==="/config")return ok({default_keyword:"Python",default_location:"Bern"});
    if(path==="/presets")return ok({});
    if(path.startsWith("/run/"))return {ok:true,body:{getReader:()=>({read:async()=>({done:true})})}};
    return ok({});
  };
  render(<App/>);
  const user=userEvent.setup();
  await screen.findByText("Engineer");
  const displayed=screen.getByLabelText("Minimum displayed fit (%)");
  await user.clear(displayed);await user.type(displayed,"60");
  await user.click(screen.getByRole("button",{name:/SCORE \(LLM\)/}));
  await waitFor(()=>assert.ok(requests.some(r=>r.url.includes("/run/analyze"))));
  const payload=JSON.parse(requests.find(r=>r.url.includes("/run/analyze")).options.body);
  assert.equal(payload.archive_below,undefined);assert.equal(payload.min_score,undefined);
  assert.equal(screen.getByLabelText("Purge maximum fit (%)").value,"10");
  assert.equal(screen.getByLabelText("Company lookup minimum fit (%)").value,"30");
  await user.click(screen.getByRole("checkbox",{name:"Not yet observed on our monitored boards"}));
  await waitFor(()=>assert.ok(requests.some(r=>r.url.includes("/jobs?")&&new URL(r.url).searchParams.get("direct_only")==="true")));
});
