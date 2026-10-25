const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const crypto = require('node:crypto');
const { chromium } = require('playwright');

let sensitiveValue = '';
const base = process.env.CLOUDSRE_BASE_URL || 'http://localhost:18080';
const mode = process.argv.find(x => x.startsWith('--case='))?.split('=')[1] || 'approve';
if (!['approve','reject','stop_investigation','stop_verification','stale_plan'].includes(mode)) throw new Error('Unsupported acceptance case');
const runId = 'd4-ui-' + mode + '-' + crypto.randomBytes(6).toString('hex');
const folder = path.join('D:/uclaude/d4-browser-runs', runId);
const linuxFolder = '/mnt/d/uclaude/d4-browser-runs/' + runId;
fs.mkdirSync(folder, { recursive: true });
const report = { case: mode, run_id: runId, passed: false, started_at: new Date().toISOString(), actions: [], evaluator_cleanup_used: false };
function save() { fs.writeFileSync(path.join(folder,'result.json'), JSON.stringify(report,null,2)); }
function record(name, data={}) { report.actions.push({ name, at: new Date().toISOString(), ...data }); save(); }
function fixture(operation, scenario='config') {
  const output=cp.execFileSync('wsl.exe',['-d','Ubuntu-24.04','--cd','/home/lee/sre-project','--','/usr/bin/env','PYTHONPATH=backend','.venv/bin/python','scripts/d4_fixture.py',operation,'--case',scenario,'--run-dir',linuxFolder],{encoding:'utf8',timeout:360000});
  return JSON.parse(output.trim().split(/\r?\n/).at(-1));
}
async function wait(fn, seconds=600) {
  const deadline=Date.now()+seconds*1000;
  while(Date.now()<deadline) { const value=await fn(); if(value)return value; await new Promise(r=>setTimeout(r,500)); }
  throw new Error('Acceptance deadline exceeded');
}
function requireTrue(condition,message) { if(!condition)throw new Error(message); }

async function main() {
  sensitiveValue=fs.readFileSync(process.env.CLOUDSRE_ACCESS_FILE || 'D:/uclaude/console-access.txt','utf8').split(/\r?\n/).find(x=>x.startsWith('访问码：'))?.slice(4);
  requireTrue(sensitiveValue,'Access code missing');
  const temp='D:/uclaude/browser-temp';fs.mkdirSync(temp,{recursive:true});process.env.TEMP=temp;process.env.TMP=temp;
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:980},reducedMotion:'reduce'});
  const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  let incidentId;let injected=false;
  async function login() {
    await page.goto(base);
    await page.getByLabel('控制台访问码',{exact:true}).fill(sensitiveValue);
    await page.getByRole('button',{name:'进入指挥台',exact:true}).click();
    await page.getByRole('button',{name:'发起调查',exact:true}).waitFor();
  }
  async function detail() {
    const response=await context.request.get(`${base}/console/incidents/${incidentId}`);
    if(response.status()===401) { await login();await page.goto(`${base}/#/incidents/${incidentId}`);return null; }
    requireTrue(response.ok(),`Detail API failed: ${response.status()}`);return response.json();
  }
  async function phase(wanted,seconds=600) {
    return wait(async()=>{const d=await detail();if(!d)return false;const state=d.incident.status;
      if(['ESCALATED','REJECTED','CANCELLED','RESOLVED'].includes(state)&&!wanted.includes(state)) throw new Error(`Unexpected terminal ${state}: ${d.latest?.payload.error || ''}`);
      return wanted.includes(state)?d:false;},seconds);
  }
  try {
    save(); await login();record('login');
    const scenario=mode==='reject'?'route':mode==='stop_investigation'?'dependency':'config';
    fixture('inject',scenario);injected=true;record('real_fault_and_alert_observed');
    await page.getByRole('button',{name:'发起调查',exact:true}).click();
    await page.getByLabel('观察到的问题',{exact:true}).fill('业务请求出现异常，请调查当前服务链路和现场证据');
    requireTrue(await page.getByRole('radio',{name:/逐次人工审批/}).isChecked(),'Manual approval must be default');
    const creation=page.waitForResponse(r=>r.url().endsWith('/console/incidents')&&r.request().method()==='POST');
    await page.getByRole('button',{name:'开始调查',exact:true}).click();
    const response=await creation;requireTrue(response.status()===201,'Task creation failed');
    incidentId=(await response.json()).incident.id;report.incident_id=incidentId;record('task_created_in_browser');
    if(mode==='stop_investigation') {
      const investigating=await phase(['INVESTIGATING']);
      await wait(async()=>{const d=await detail();return d?.latest?.payload.diagnoses?.length?d:false;},60);
      await page.getByRole('button',{name:'停止任务',exact:true}).click();
      await page.getByLabel('停止原因',{exact:true}).fill('演示在调查期间停止，禁止后续变更');
      await page.getByRole('button',{name:'确认停止后续任务',exact:true}).click();record('stop_requested_during_investigation');
      const end=await phase(['CANCELLED'],90);report.final=end;
      requireTrue(!end.latest.payload.action_intent,'Stopped investigation produced executable action');
      requireTrue(end.latest.payload.diagnoses.every(d=>!['prepared','running','cancelling'].includes(d.state)),'Diagnostic process not acknowledged stopped');
      requireTrue(fixture('inspect').matches_injected,'Task changed fault after investigation stop');
    } else {
      const ready=await phase(['WAITING_APPROVAL']);record('awaiting_bound_approval',{run_id:ready.latest.id,plan_hash:ready.plan_hash});
      requireTrue(fixture('inspect').matches_injected,'Mutation occurred before approval');
      report.waiting=ready;save();
      if(mode==='approve') {
        cp.execFileSync('wsl.exe',['-d','Ubuntu-24.04','--','docker','restart','cloudsre-worker-d4-1','cloudsre-worker-d4-2'],{encoding:'utf8',timeout:45000});
        requireTrue((await detail()).incident.status==='WAITING_APPROVAL','Waiting plan did not survive worker restart');
        record('waiting_survived_worker_restart');
      }
      if(process.argv.includes('--pause-at-plan')) {
        console.log(JSON.stringify({phase:'WAITING_APPROVAL',folder,incident_id:incidentId}));
        await wait(()=>fs.existsSync(path.join(folder,'continue')),1800);
        const session=await context.request.get(base+'/console/session');if(session.status()===401)await login();
        await page.goto(`${base}/#/incidents/${incidentId}`);
      }
      if(mode==='reject') {
        await page.getByRole('button',{name:'拒绝此方案',exact:true}).click();
        await page.getByLabel('拒绝原因',{exact:true}).fill('本轮只验证拒绝流程，保持未批准的变更不执行');
        await page.getByRole('button',{name:'确认拒绝',exact:true}).click();record('plan_rejected_in_browser');
        const end=await phase(['REJECTED']);report.final=end;
        requireTrue(fixture('inspect').matches_injected,'Rejected plan changed resource');
      } else {
        if(mode==='stale_plan') {
          cp.execFileSync('wsl.exe',['-d','Ubuntu-24.04','--','/home/lee/sre-tools/bin/kubectl','--context','kind-sre-dev','-n','sre-demo','annotate','deployment/order','cloudsre/review-interference='+crypto.randomBytes(5).toString('hex'),'--overwrite'],{encoding:'utf8',timeout:30000});
          record('resource_version_changed_after_plan');
        }
        await page.getByRole('checkbox',{name:/我已核对目标、证据与版本/}).check();
        await page.getByRole('button',{name:'批准并执行',exact:true}).click();record('exact_plan_approved_in_browser');
        if(mode==='stale_plan') {
          const end=await phase(['ESCALATED']);report.final=end;
          requireTrue(end.latest.payload.action_result.state==='CONFLICT','Stale plan was not refused');
          requireTrue(fixture('inspect').matches_injected,'Stale approval bypassed version guard');
        } else {
          await phase(['VERIFYING']);
          await page.getByRole('tab',{name:'业务验收',exact:true}).click();
          const sampling=await wait(async()=>{const d=await detail();return d?.verification_live?.load_requests>=3?d:false;},90);
          record('live_business_samples_visible',{load_requests:sampling.verification_live.load_requests});
          if(mode==='stop_verification') {
            await page.getByRole('button',{name:'停止任务',exact:true}).click();
            await page.getByLabel('停止原因',{exact:true}).fill('停止当前验证，保留部分样本，不宣称已恢复');
            await page.getByRole('button',{name:'确认停止后续任务',exact:true}).click();record('stop_requested_during_verification');
            const end=await phase(['CANCELLED'],90);report.final=end;
            requireTrue(end.latest.payload.quality?.interrupted===true&&end.latest.payload.quality.passed===false,'Partial window wrongly passed');
            requireTrue(end.latest.payload.stop_action_observation==='APPLIED','Applied action was not reconciled');
            requireTrue(end.verification_claim==='not_verified_recovery','Cancellation falsely reported recovery');
          } else {
            const end=await phase(['RESOLVED']);report.final=end;
            const q=end.latest.payload.quality;
            requireTrue(q.passed&&q.duration_seconds>=120&&q.load_requests>=120&&q.business_checks>=12&&q.errors===0,'Full business gates failed');
            requireTrue(end.decision?.decision==='approve','Human approval audit missing');
            requireTrue(fixture('inspect').matches_healthy,'Resource not actually restored');
          }
        }
      }
    }
    requireTrue(errors.length===0,'Browser runtime exceptions: '+errors.join('; '));
    report.browser_exceptions=errors;report.passed=true;record('acceptance_passed');
  } catch(e) {
    report.error=String(e.message).replaceAll(sensitiveValue,'[REDACTED]');record('acceptance_failed');
    if(incidentId) {
      try { const d=await detail();report.final=d;if(!['RESOLVED','ESCALATED','CANCELLED','REJECTED'].includes(d?.incident.status)) {
        await context.request.post(`${base}/console/incidents/${incidentId}/stop`,{headers:{'X-CloudSRE-Request':'console'},data:{reason:'评测失败后的明确停止请求'}});
        await phase(['CANCELLED','ESCALATED','RESOLVED','REJECTED'],60);
      }} catch { cp.spawnSync('wsl.exe',['-d','Ubuntu-24.04','--','docker','stop','cloudsre-worker-d4-1','cloudsre-worker-d4-2'],{encoding:'utf8',timeout:45000});report.evaluator_force_stopped_workers=true; }
    }
  } finally {
    if(injected) { try { const cleanup=fixture('cleanup');report.evaluator_cleanup_used=cleanup.evaluator_cleanup_used; } catch(e) { report.cleanup_error=String(e.message).replaceAll(sensitiveValue,'[REDACTED]');report.passed=false; } }
    report.ended_at=new Date().toISOString();save();await context.close();await browser.close();
    console.log(JSON.stringify({run_id:runId,passed:report.passed,evaluator_cleanup_used:report.evaluator_cleanup_used,error:report.error,folder}));
  }
  if(!report.passed)process.exitCode=1;
}
main().catch(e=>{console.error(String(e.message).replaceAll(sensitiveValue,'[REDACTED]'));process.exitCode=1;});
