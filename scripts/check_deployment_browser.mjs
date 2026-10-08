/* Real TLS proxy, secure login, process restart and offline restore drill. */
import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {mkdtemp, mkdir, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import net from 'node:net';
import {chromium} from 'playwright';

const temporary=await mkdtemp(join(tmpdir(),'aegis-deployment-'));
const directory=resolve('data/evaluation/deployment');
await mkdir(directory,{recursive:true});
const python=resolve('.venv/bin/python');
async function port(){const server=net.createServer();await new Promise(done=>server.listen(0,'127.0.0.1',done));const value=server.address().port;await new Promise(done=>server.close(done));return value;}
const appPort=await port(), tlsPort=await port(), origin=`https://localhost:${tlsPort}`;
let app, proxy, browser, logs='';
const checks=[];
const storage=join(temporary,'storage');
const setup=spawnSync(python,['-c',"import os; from aegis.app.store import Store; Store(os.environ['AEGIS_STORAGE']).add_user('owner@example.test','Owner','deployment-only-test-password')"],{env:{...process.env,AEGIS_STORAGE:storage},encoding:'utf8'});
assert.equal(setup.status,0,setup.stderr);
function start(root){const child=spawn(python,['-m','aegis.app.server','--storage',root,'serve','--port',String(appPort)],{env:{...process.env,AEGIS_COOKIE_SECURE:'1',AEGIS_ALLOWED_HOSTS:'localhost,127.0.0.1',AEGIS_PUBLIC_ORIGIN:origin},stdio:['ignore','ignore','pipe']});child.stderr.on('data',value=>{logs+=value;});return child;}
async function stop(child){if(!child||child.exitCode!==null)return;child.kill('SIGINT');await new Promise(done=>{child.once('exit',done);setTimeout(()=>{child.kill('SIGKILL');done();},10000).unref();});}
async function until(fn){const deadline=Date.now()+30000;while(Date.now()<deadline){try{if(await fn())return;}catch{}await new Promise(done=>setTimeout(done,150));}throw Error('Timed out: '+logs);}
try{
  const configuration=join(temporary,'Caddyfile');
  await writeFile(configuration,`{
    admin off
    skip_install_trust
    auto_https disable_redirects
  }
  ${origin} {
    tls internal
    reverse_proxy 127.0.0.1:${appPort}
  }
  `);
  proxy=spawn(resolve('data/runtime/caddy/caddy'),['run','--config',configuration,'--adapter','caddyfile'],{env:{...process.env,XDG_DATA_HOME:join(temporary,'caddy-data'),XDG_CONFIG_HOME:join(temporary,'caddy-config')},stdio:['ignore','ignore','pipe']});proxy.stderr.on('data',value=>{logs+=value;});
  app=start(storage);
  await until(async()=>(await fetch(`http://127.0.0.1:${appPort}/health/live`)).ok);
  browser=await chromium.launch({executablePath:process.env.AEGIS_BROWSER||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({ignoreHTTPSErrors:true});
  const page=await context.newPage();
  async function login(){await page.goto(origin);await page.locator('#email').fill('owner@example.test');await page.locator('#password').fill('deployment-only-test-password');await page.locator('#sign-in').click();await page.locator('#new-project').waitFor({state:'visible'});}
  await login();
  const cookie=(await context.cookies()).find(value=>value.name==='__Host-aegis_session');
  assert.ok(cookie?.secure&&cookie.httpOnly&&cookie.sameSite==='Strict');
  assert.equal((await context.request.get(origin+'/api/system')).status(),200);
  await until(async()=>(await context.request.get(origin+'/health/ready')).status()===200);
  checks.push('Caddy TLS proxy, secure HttpOnly host cookie, private login and live readiness');
  await page.locator('#new-project').click();await page.locator('#project-name').fill('Recovery drill');await page.locator('#project-save').click();await page.locator('#project-dialog').waitFor({state:'hidden'});
  await stop(app);app=start(storage);
  await until(async()=>(await fetch(`http://127.0.0.1:${appPort}/health/live`)).ok);
  await page.reload();await page.locator('#new-project').waitFor({state:'visible'});
  assert.ok((await (await context.request.get(origin+'/api/projects')).json()).projects.some(p=>p.name==='Recovery drill'));
  checks.push('Application restart preserves secure sessions and private projects');
  await page.goto('about:blank');
  await stop(app);
  const archive=join(temporary,'backup.zip'), restored=join(temporary,'restored');
  for(const [root,command] of [[storage,'backup'],[restored,'restore']]){
    const result=spawnSync(python,['-m','aegis.app.server','--storage',root,command,archive],{encoding:'utf8'});assert.equal(result.status,0,result.stderr);
  }
  app=start(restored);
  await until(async()=>(await fetch(`http://127.0.0.1:${appPort}/health/live`)).ok);
  assert.equal((await context.request.get(origin+'/api/session')).status(),401);
  await login();
  assert.ok((await (await context.request.get(origin+'/api/projects')).json()).projects.some(p=>p.name==='Recovery drill'));
  checks.push('Offline verified backup restores projects while revoking pre-restore sessions');
  const report={status:'passed',created_at:new Date().toISOString(),checks,certificate:'Local test CA; public certificate issuance is not tested'};
  await writeFile(join(directory,'report.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
}finally{if(browser)await browser.close();await stop(app);await stop(proxy);await rm(temporary,{recursive:true,force:true});}
