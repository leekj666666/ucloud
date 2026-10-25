const fs=require('node:fs');
const path=require('node:path');
const cp=require('node:child_process');
const crypto=require('node:crypto');
const {chromium}=require('playwright');
let secret='';
const sourceName=process.argv.find(x=>x.startsWith('--source='))?.slice(9);
if(!/^d4-ui-approve-[a-f0-9]{12}$/.test(sourceName||''))throw new Error('An exact interrupted approval run is required');
const sourceFolder=path.join('D:/uclaude/d4-browser-runs',sourceName);
const original=JSON.parse(fs.readFileSync(path.join(sourceFolder,'result.json'),'utf8'));
const runId='d4-ui-approve-resumed-'+crypto.randomBytes(6).toString('hex');
const folder=path.join('D:/uclaude/d4-browser-runs',runId);fs.mkdirSync(folder);
const report={case:'approve',run_id:runId,passed:false,started_at:new Date().toISOString(),source_case:sourceName,resumed_after_interruption:true,incident_id:original.incident_id,evaluator_source:original.evaluator_source,actions:[],evaluator_cleanup_used:false};
function save(){fs.writeFileSync(path.join(folder,'result.json'),JSON.stringify(report,null,2));}
function record(name,data={}){report.actions.push({name,at:new Date().toISOString(),...data});save();}
function check(value,message){if(!value)throw new Error(message);}
async function main(){
  secret=fs.readFileSync('D:/uclaude/console-access.txt','utf8').split(/\r?\n/).find(x=>x.startsWith('访问码：'))?.slice(4);
  process.env.TEMP='D:/uclaude/browser-temp';process.env.TMP='D:/uclaude/browser-temp';
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const context=await browser.newContext();const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const base='http://localhost:18080';const ident=original.incident_id;
  const detail=async()=>{const response=await context.request.get(`${base}/console/incidents/${ident}`);check(response.ok(),'Detail unavailable');return response.json();};
  try{
    save();await page.goto(base);await page.getByLabel('控制台访问码',{exact:true}).fill(secret);await page.getByRole('button',{name:'进入指挥台',exact:true}).click();
    await page.getByRole('button',{name:'发起调查',exact:true}).waitFor();await page.goto(`${base}/#/incidents/${ident}`);
    const before=await detail();report.before=before;
    check(before.incident.status==='VERIFYING','Interrupted task is not in verification');
    check(before.latest.id===original.waiting.latest.id&&before.plan_hash===original.waiting.plan_hash,'Original run or plan changed');
    check(before.decision?.decision==='approve','Durable human approval missing');
    await page.getByText('操作员已批准本方案',{exact:true}).waitFor();record('original_plan_and_human_approval_retained');
    cp.execFileSync('docker',['start','cloudsre-worker-d4-1','cloudsre-worker-d4-2'],{encoding:'utf8',timeout:45000});record('workers_resumed_existing_workflow');
    await page.getByRole('tab',{name:'业务验收',exact:true}).click();
    const deadline=Date.now()+360000;let end;
    while(Date.now()<deadline){
      const current=await detail();
      if(['ESCALATED','CANCELLED','REJECTED'].includes(current.incident.status))throw new Error('Unexpected terminal: '+current.incident.status+' '+(current.latest.payload.error||''));
      if(current.incident.status==='RESOLVED'){end=current;break;}
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    check(end,'Resumed verification deadline exceeded');report.final=end;
    check(end.latest.id===before.latest.id,'Resume created a replacement workflow');
    const q=end.latest.payload.quality;check(q.passed&&q.duration_seconds>=120&&q.load_requests>=120&&q.business_checks>=12&&q.errors===0,'Incomplete business window');
    check(end.latest.payload.verification_attempts>before.latest.payload.verification_attempts,'Partial window was reused');
    check(end.verification_claim==='verified_recovery','Authoritative recovery missing');
    await page.reload();await page.getByRole('tab',{name:'业务验收',exact:true}).click();await page.getByText('业务恢复已确认',{exact:true}).waitFor();
    record('fresh_complete_window_and_authoritative_ui_confirmed',{attempt:end.latest.payload.verification_attempts,requests:q.load_requests,business_checks:q.business_checks});
    for(const format of ['json','markdown']){const response=await context.request.get(`${base}/console/incidents/${ident}/export?format=${format}`);check(response.ok(),'Export failed');fs.writeFileSync(path.join(folder,'export.'+(format==='json'?'json':'md')),await response.body());}
    const fixtureOutput=cp.execFileSync('wsl.exe',['-d','Ubuntu-24.04','--cd','/home/lee/sre-project','--','/usr/bin/env','PYTHONPATH=backend','.venv/bin/python','scripts/d4_fixture.py','cleanup','--run-dir','/mnt/d/uclaude/d4-browser-runs/'+sourceName],{encoding:'utf8',timeout:60000});
    const cleanup=JSON.parse(fixtureOutput.trim().split(/\r?\n/).at(-1));check(!cleanup.evaluator_cleanup_used,'Continuation required evaluator repair');
    check(errors.length===0,'Browser exceptions detected');report.browser_exceptions=errors;report.passed=true;record('resumed_acceptance_passed');
  }catch(e){report.error=String(e.message).replaceAll(secret,'[REDACTED]');record('resumed_acceptance_failed');}
  finally{report.ended_at=new Date().toISOString();save();await context.close();await browser.close();console.log(JSON.stringify({run_id:runId,passed:report.passed,error:report.error,folder}));}
  if(!report.passed)process.exitCode=1;
}
main().catch(e=>{console.error(String(e.message).replaceAll(secret,'[REDACTED]'));process.exitCode=1;});
