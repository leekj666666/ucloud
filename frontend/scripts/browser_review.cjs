const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const AxeBuilder=require('@axe-core/playwright').default;
let secret='';
async function main(){
 const base='http://localhost:18080';
 secret=fs.readFileSync('D:/uclaude/console-access.txt','utf8').split(/\r?\n/).find(x=>x.startsWith('访问码：')).slice(4);
 const incident=process.argv.find(x=>x.startsWith('--incident='))?.slice(11);
 if(!incident)throw new Error('Incident ID required');
 const round=process.argv.find(x=>x.startsWith('--round='))?.slice(8)||'1';
 const out=path.resolve('.impeccable/review');fs.mkdirSync(out,{recursive:true});
 const tmp='D:/uclaude/browser-temp';fs.mkdirSync(tmp,{recursive:true});process.env.TEMP=tmp;process.env.TMP=tmp;
 const browser=await chromium.launch({channel:'chrome',headless:true});const findings=[];
 try{
  for(const [name,viewport] of [['desktop',{width:1440,height:980}],['mobile',{width:390,height:844}]]){
   const context=await browser.newContext({viewport,reducedMotion:'reduce'});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.goto(base);await page.getByLabel('控制台访问码',{exact:true}).waitFor();
   await page.screenshot({path:path.join(out,`${name}-login-r${round}.png`),fullPage:true});
   const login=await context.request.post(base+'/console/session',{headers:{'X-CloudSRE-Request':'console'},data:{token:secret}});if(!login.ok())throw new Error('Review login failed');
   await page.goto(`${base}/#/incidents/${incident}`);await page.reload();
   await page.getByRole('button',{name:'批准并执行',exact:true}).waitFor();
   await page.screenshot({path:path.join(out,`${name}-r${round}.png`),fullPage:name==='mobile'});
   if(round==='1')await page.screenshot({path:path.join(out,`${name}.png`),fullPage:name==='mobile'});
   const axe=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
   const dimensions=await page.evaluate(()=>({viewport:window.innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth}));
   findings.push({viewport:name,screen:'waiting_approval',dimensions,violations:axe.violations.map(v=>({id:v.id,impact:v.impact,description:v.description,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))})),browser_errors:errors});
   await page.getByRole('tab',{name:'业务验收',exact:true}).click();await page.screenshot({path:path.join(out,`${name}-verification-r${round}.png`),fullPage:name==='mobile'});
   await page.getByRole('tab',{name:'报告与用量',exact:true}).click();await page.screenshot({path:path.join(out,`${name}-report-r${round}.png`),fullPage:name==='mobile'});
   await context.close();
  }
 }finally{await browser.close();}
 fs.writeFileSync(path.join(out,`review-r${round}.json`),JSON.stringify(findings,null,2));
 console.log(JSON.stringify(findings.map(x=>({viewport:x.viewport,violations:x.violations.map(v=>v.id),dimensions:x.dimensions,browser_errors:x.browser_errors}))));
}
main().catch(e=>{console.error(String(e.message).replaceAll(secret,'[REDACTED]'));process.exitCode=1;});
