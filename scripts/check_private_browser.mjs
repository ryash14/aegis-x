/* Real private-app flows in Chrome, with two isolated authenticated contexts. */
import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {mkdtemp, mkdir, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import net from 'node:net';
import {chromium} from 'playwright';

const storage=await mkdtemp(join(tmpdir(),'aegis-private-browser-'));
const directory=resolve('data/evaluation/private-browser');
await mkdir(directory,{recursive:true});
const provision=spawnSync(resolve('.venv/bin/python'),['-c',"import os; from aegis.app.store import Store; s=Store(os.environ['AEGIS_STORAGE']); s.add_user('alice@example.test','Alice','browser-only-test-password'); s.add_user('bob@example.test','Bob','browser-only-test-password')"],{env:{...process.env,AEGIS_STORAGE:storage},encoding:'utf8'});
assert.equal(provision.status,0,provision.stderr);
const reservation=net.createServer();
await new Promise(accept=>reservation.listen(0,'127.0.0.1',accept));
const port=reservation.address().port;
await new Promise(accept=>reservation.close(accept));
const base=`http://127.0.0.1:${port}`;
const child=spawn(resolve('.venv/bin/python'),['-m','aegis.app.server','--storage',storage,'serve','--port',String(port)],{stdio:['ignore','ignore','pipe']});
let logs='';child.stderr.on('data',buffer=>{logs+=buffer;});
async function until(fn,timeout=90000){const end=Date.now()+timeout;while(Date.now()<end){if(child.exitCode!==null)throw Error('Server stopped: '+logs);const result=await fn();if(result)return result;await new Promise(accept=>setTimeout(accept,100));}throw Error('Operation timed out');}
let browser;
try {
  await until(async()=>{try{return (await fetch(base+'/health/live')).ok;}catch{return false;}},15000);
  browser=await chromium.launch({executablePath:process.env.AEGIS_BROWSER||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:1600,height:1100}});
  const page=await context.newPage();page.setDefaultTimeout(60000);
  const errors=[],remote=[],checks=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(/^https?:/.test(request.url())&&new URL(request.url()).origin!==base)remote.push(request.url());});
  async function signin(target,email){await target.goto(base);await target.locator('#email').fill(email);await target.locator('#password').fill('browser-only-test-password');await target.locator('#sign-in').click();await target.locator('#new-project').waitFor({state:'visible'});await target.waitForFunction(()=>!document.getElementById('new-project').disabled&&document.getElementById('project-select').options.length>0);}
  async function data(path,project){const response=await context.request.get(base+path,{headers:project?{'X-Aegis-Project':project}:{}});assert.ok(response.ok(),await response.text());return response.json();}
  async function select(name){await page.locator(`.document-card[title="${name}"]`).click();await page.waitForFunction(name=>document.getElementById('document-name').textContent===name,name);}
  async function tab(method){await page.locator(`[data-method="${method}"]`).click();await page.waitForFunction(()=>!document.getElementById('output').textContent.includes('Loading…'));}
  await page.goto(base);assert.ok(page.url().endsWith('/login'));
  await page.locator('#email').fill('alice@example.test');await page.locator('#password').fill('incorrect');await page.locator('#sign-in').click();await page.locator('#login-error').waitFor({state:'visible'});assert.match(await page.locator('#login-error').textContent(),/incorrect/);
  await signin(page,'alice@example.test');checks.push('Real login, incorrect password feedback, session cookie and private empty state');
  await page.locator('#new-project').click();await page.locator('#project-name').fill('Subsystem evidence');await page.locator('#project-description').fill('Public engineering document fixtures');await page.locator('#project-save').click();await page.locator('#project-dialog').waitFor({state:'hidden'});
  const project=await page.locator('#project-select').inputValue();assert.ok(project);checks.push('Create a named private project');
  await page.locator('#settings-button').click();await page.locator('#chunk-size').fill('600');await page.locator('#chunk-overlap').fill('80');await page.locator('#revision').fill('Rev A');await page.locator('#document-role').selectOption('baseline');await page.locator('#settings-button').click();
  const files=['reports/nasa-systems-engineering-handbook.pdf','docling/docx/word_tables.docx','docling/ocr/ocr_test_rotated_90.pdf','docling/pdf_password/2206.01062_pg3.pdf'].map(path=>resolve('data/test-corpus',path));
  await page.locator('#file-input').setInputFiles(files);
  const library=await until(async()=>{const value=await data('/api/documents',project);return value.total===4&&value.documents.every(item=>['ready','failed'].includes(item.status))&&value;});
  assert.equal(library.documents.filter(d=>d.status==='ready').length,3);assert.equal(library.documents.filter(d=>d.status==='failed').length,1);assert.ok(library.documents.every(d=>d.project_id===project&&d.role==='baseline'&&d.revision==='Rev A'));checks.push('Multi-upload, bounded processing, revision metadata and isolated encrypted failure');
  await until(async()=>{const value=await data('/api/search/status',project);return value.dense_available&&value.documents.length===3&&value.documents.every(item=>item.chunks===item.embedded);},180000);
  for(const mode of ['sparse','dense','hybrid']){const found=await data('/api/search?q=systems%20engineering&mode='+mode,project);assert.ok(found.results.length);assert.ok(found.results.every(hit=>library.documents.some(document=>document.id===hit.job_id)));assert.ok(found.context_chars<=found.context_budget);}
  await page.locator('#evidence-query').fill('systems engineering');await page.locator('#evidence-submit').click();await page.locator('.evidence-result').first().waitFor({state:'visible'});
  const resultText=await page.locator('.evidence-result p').first().textContent();assert.ok(resultText.length);
  const source=await page.locator('.evidence-result a').first().getAttribute('href');await page.goto(base+source);await page.waitForFunction(()=>document.getElementById('output').textContent.length>0&&document.querySelector('[data-method="chunks"]').getAttribute('aria-selected')==='true');
  assert.ok((await page.locator('#output').textContent()).includes(resultText.trim()));
  await page.locator('#evidence-role').selectOption('candidate');await page.locator('#evidence-query').fill('systems engineering');await page.locator('#evidence-submit').click();await page.waitForFunction(()=>document.getElementById('evidence-status').textContent.startsWith('0 results'));await page.locator('#evidence-role').selectOption('');
  checks.push('Automatic persistent embeddings, scoped sparse/dense/hybrid retrieval, role filter, context bounds and exact chunk navigation');
  await select('word_tables.docx');await page.locator('#docx-preview table').first().waitFor({state:'visible'});await tab('chunks');await page.locator('#chunk-select option').first().waitFor({state:'attached'});await page.waitForFunction(()=>document.querySelector('#docx-preview .source-active'));checks.push('DOCX structure, table chunks and source highlighting');
  await select('ocr_test_rotated_90.pdf');await tab('ocr');await page.waitForFunction(()=>document.getElementById('output').textContent.includes('Docling bundles'));await page.locator('#output .text-block').first().hover();assert.ok(await page.locator('.source-box').count());await tab('baseline');assert.match(await page.locator('#output').textContent(),/No native text/);checks.push('Rotated local OCR, source coordinates and separate native text');
  await select('nasa-systems-engineering-handbook.pdf');await page.locator('#page-number').fill('11');await page.locator('#page-number').press('Tab');await page.waitForFunction(()=>document.getElementById('output').textContent.includes('This handbook'));await tab('layout');await page.locator('#output .text-block').first().hover();assert.ok(await page.locator('.source-box').count());await tab('normalized');await page.waitForFunction(()=>document.getElementById('output').textContent.includes('This handbook'));await tab('chunks');await page.waitForFunction(()=>document.getElementById('chunk-select').options.length>0);await page.locator('#pdf-toggle').click();await page.locator('#pdf-frame').waitFor({state:'visible'});checks.push('Large PDF, lazy page preview, original viewer, all extraction methods and chunk provenance');
  await page.locator('#document-settings').click();await page.locator('#edit-revision').fill('Rev B');await page.locator('#edit-role').selectOption('candidate');await page.locator('#revision-save').click();await page.locator('#revision-dialog').waitFor({state:'hidden'});assert.match(await page.locator('#document-meta').textContent(),/candidate.*Rev B/);checks.push('Revision and role editing preserves source identity');
  await page.screenshot({path:join(directory,'desktop.png')});await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:join(directory,'mobile.png'),fullPage:true});await page.setViewportSize({width:1600,height:1100});checks.push('Desktop and mobile source-inspection layout');
  await page.locator('#new-project').click();await page.locator('#project-name').fill('Separate collection');await page.locator('#project-save').click();await page.locator('#project-dialog').waitFor({state:'hidden'});const second=await page.locator('#project-select').inputValue();assert.notEqual(second,project);assert.equal((await data('/api/documents',second)).total,0);await page.locator('#project-select').selectOption(project);await page.locator('.document-card').first().waitFor({state:'visible'});checks.push('Project switching separates document libraries');
  const bobContext=await browser.newContext();const bobPage=await bobContext.newPage();await signin(bobPage,'bob@example.test');assert.equal(await bobPage.locator('#project-select option').count(),1);assert.equal((await bobContext.request.get(base+'/api/projects')).status(),200);const bobProjects=await (await bobContext.request.get(base+'/api/projects')).json();assert.equal(bobProjects.projects.length,0);assert.equal((await bobContext.request.get(base+'/api/search?q=systems&project_id='+project)).status(),404);assert.equal((await bobContext.request.get(base+'/api/search/status?project_id='+project)).status(),404);
  const victim=library.documents.find(d=>d.status==='ready');for(const suffix of ['','/original','/summary','/pages/1/image','/chunks/0'])assert.equal((await bobContext.request.get(base+'/api/documents/'+victim.id+suffix)).status(),404);await bobContext.close();checks.push('Second browser account cannot list or read another user’s projects, sources or evidence');
  await page.reload();await page.locator('.document-card').first().waitFor({state:'visible'});assert.equal((await data('/api/documents',project)).total,4);await select('2206.01062_pg3.pdf');await page.locator('#job-error').waitFor({state:'visible'});await page.locator('#retry').click();await until(async()=>{const value=await data('/api/documents',project);return value.documents.find(d=>d.name==='2206.01062_pg3.pdf')?.status==='failed';});page.once('dialog',dialog=>dialog.accept());await page.locator('#remove').click();await until(async()=>(await data('/api/documents',project)).total===3);checks.push('Refresh persistence, explicit failed-job retry and permanent deletion');
  await page.locator('#logout').click();await page.locator('#email').waitFor({state:'visible'});assert.equal((await context.request.get(base+'/api/session')).status(),401);checks.push('Sign-out revokes server-side access');
  assert.deepEqual(errors,[]);assert.deepEqual(remote,[]);
  const report={status:'passed',created_at:new Date().toISOString(),checks,console_errors:errors,external_http_requests:remote};await writeFile(join(directory,'report.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));await context.close();
} finally {
  if(browser)await browser.close();child.kill('SIGINT');await new Promise(accept=>{if(child.exitCode!==null)return accept();child.once('exit',accept);setTimeout(()=>{child.kill('SIGKILL');accept();},10000).unref();});await rm(storage,{recursive:true,force:true});
}
