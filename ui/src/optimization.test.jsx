import test,{afterEach} from "node:test";
import assert from "node:assert/strict";
import {JSDOM} from "jsdom";
import React from "react";

const dom=new JSDOM("<!doctype html><html><body></body></html>",{url:"http://test/"});
globalThis.window=dom.window;globalThis.document=dom.window.document;
globalThis.navigator=dom.window.navigator;globalThis.HTMLElement=dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const {render,screen,cleanup,waitFor}=await import("@testing-library/react");
const {default:userEvent}=await import("@testing-library/user-event");
const {default:MatchingSettingsView}=await import("./MatchingSettingsView");
const {default:ReviewQueue}=await import("./ReviewQueue");
const {default:VacancyEvidence}=await import("./VacancyEvidence");
const {vacancyQuery}=await import("./vacancyQuery");
const {default:App}=await import("./App");
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
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
