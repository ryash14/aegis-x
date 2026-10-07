/* Real multi-upload and inspection flows against Chrome; no mocked API. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, mkdir, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {chromium} from 'playwright';

const storage = await mkdtemp(join(tmpdir(), 'aegis-browser-'));
const reportDirectory = resolve('data/evaluation/browser');
await mkdir(reportDirectory, {recursive: true});
const child = spawn(resolve('.venv/bin/python'), ['-m', 'aegis.workspace', '--storage', storage, '--port', '0'], {stdio: ['ignore', 'pipe', 'pipe']});
let serverOutput = '';
const base = await new Promise((accept, reject) => {
  const timer = setTimeout(() => reject(Error('Workspace did not start')), 15000);
  child.stdout.on('data', buffer => { serverOutput += buffer; const match = serverOutput.match(/http:\/\/127\.0\.0\.1:\d+/); if (match) {clearTimeout(timer);accept(match[0]);} });
  child.once('exit', code => reject(Error('Workspace exited: '+code)));
});
const browser = await chromium.launch({executablePath: process.env.AEGIS_BROWSER || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
const page = await browser.newPage({viewport: {width: 1600, height: 1100}});
page.setDefaultTimeout(60000);
const errors = [], remote = [], checks = [];
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => {if (/^https?:/.test(request.url()) && new URL(request.url()).origin !== base) remote.push(request.url());});
async function until(fn, timeout=90000) {const end=Date.now()+timeout;while(Date.now()<end){const value=await fn();if(value)return value;await new Promise(resolve=>setTimeout(resolve,150));}throw Error('Check timed out');}
async function data(path) {return (await fetch(base+path)).json();}
async function select(name) {await page.locator(`.document-card[title="${name}"]`).click();await page.waitForFunction(name=>document.getElementById('document-name').textContent===name,name);}
async function tab(method) {await page.locator(`[data-method="${method}"]`).click();await page.waitForFunction(()=>!document.getElementById('output').textContent.includes('Loading…'));}
try {
  await page.goto(base);await page.locator('#choose').waitFor({state:'visible'});
  await page.locator('#settings-button').click();await page.locator('#chunk-size').fill('600');await page.locator('#chunk-overlap').fill('80');await page.locator('#settings-button').click();
  const files = ['reports/nasa-systems-engineering-handbook.pdf', 'docling/docx/word_tables.docx', 'docling/ocr/ocr_test_rotated_90.pdf', 'docling/pdf_password/2206.01062_pg3.pdf'].map(path=>resolve('data/test-corpus',path));
  await page.locator('#file-input').setInputFiles(files);
  await until(async()=>{const library=await data('/api/documents');return library.total===4&&library.documents.every(item=>['ready','failed'].includes(item.status))&&library;});
  const library=await data('/api/documents');assert.equal(library.documents.filter(item=>item.status==='ready').length,3);assert.equal(library.documents.filter(item=>item.status==='failed').length,1);checks.push('Multi-file upload, isolated encrypted failure, file sizes and live job completion');
  await page.goto(base+'/docs/retrieval.html');await page.waitForFunction(()=>document.getElementById('stats').textContent.includes('indexed documents'));await page.locator('#index').click();await page.waitForFunction(()=>document.getElementById('status').textContent.startsWith('Updated'));await page.locator('#query').fill('systems engineering');await page.locator('#mode').selectOption('all');await page.locator('#search button').click();await page.locator('#results article').first().waitFor();
  const evidence=await data('/api/search?q=systems%20engineering&mode=all');assert.ok(evidence.results.length>0);assert.ok(evidence.results[0].chunk.mappings.length>0);const sourceLink=await page.locator('#results article a').first().getAttribute('href');await page.goto(base+sourceLink);await page.waitForFunction(()=>!document.getElementById('ready').hidden);assert.equal(await page.locator('#document-name').textContent(),evidence.results[0].name);assert.equal(Number(await page.locator('#page-number').inputValue()),evidence.results[0].pages[0]);checks.push('BM25 indexing, ranked evidence, source mappings and exact source-page navigation');
  await select('word_tables.docx');await page.locator('#docx-preview table').first().waitFor({state:'visible'});await tab('chunks');await page.locator('#chunk-select option').first().waitFor({state:'attached'});await page.waitForFunction(()=>document.querySelector('#docx-preview .source-active'));checks.push('DOCX tables, chunks and XML source highlighting');
  await select('ocr_test_rotated_90.pdf');await tab('ocr');await page.waitForFunction(()=>document.querySelector('#output').textContent.includes('Docling bundles'));await page.locator('#output .text-block').first().hover();assert.ok(await page.locator('.source-box').count()>0);await tab('baseline');assert.match(await page.locator('#output').textContent(),/No native text/);await tab('chunks');await page.waitForFunction(()=>document.querySelector('#output').textContent.includes('Docling'));checks.push('Rotated OCR, confidence, source boxes and separate native text');
  await select('nasa-systems-engineering-handbook.pdf');await page.locator('#page-number').fill('11');await page.locator('#page-number').press('Tab');await page.waitForFunction(()=>document.getElementById('preview').src.includes('/pages/11/image')&&document.getElementById('output').textContent.includes('This handbook'));await tab('baseline');await tab('layout');await page.locator('#output .text-block').first().hover();assert.ok(await page.locator('.source-box').count()>0);await tab('normalized');await page.waitForFunction(()=>document.getElementById('output').textContent.includes('This handbook'));await tab('chunks');await page.waitForFunction(()=>document.getElementById('chunk-select').options.length>0);
  assert.equal(await page.evaluate(()=>state.summary.chunk_config.max_chars),600);assert.ok(await page.evaluate(()=>Array.from(state.chunk.text).length<=600));await page.locator('#pdf-toggle').click();await page.locator('#pdf-frame').waitFor({state:'visible'});assert.match(await page.locator('#pdf-frame').getAttribute('src'),/original#page=11/);await page.locator('#pdf-toggle').click();await page.waitForFunction(()=>document.getElementById('preview').naturalWidth>0);checks.push('Large PDF, page navigation, all methods, configurable chunks and original PDF viewer');
  await page.screenshot({path:join(reportDirectory,'desktop.png')});
  await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:join(reportDirectory,'mobile.png'),fullPage:true});checks.push('Desktop and mobile layout');
  await page.setViewportSize({width:1600,height:1100});await select('2206.01062_pg3.pdf');await page.locator('#job-error').waitFor({state:'visible'});await page.locator('#retry').click();await until(async()=>{const value=await data('/api/documents');return value.documents.find(item=>item.name==='2206.01062_pg3.pdf')?.status==='failed';});
  await page.reload();await page.locator('.document-card').first().waitFor({state:'visible'});assert.equal((await data('/api/documents')).total,4);await select('2206.01062_pg3.pdf');page.once('dialog',dialog=>dialog.accept());await page.locator('#remove').click();await until(async()=>(await data('/api/documents')).total===3);checks.push('Persistent library, explicit retry and soft removal');
  await page.locator('#file-input').setInputFiles({name:'unsupported.txt',mimeType:'text/plain',buffer:Buffer.from('Unsupported')});await page.waitForFunction(()=>document.getElementById('toast').textContent.includes('PDF or DOCX only'));assert.equal((await data('/api/documents')).total,3);checks.push('Unsupported upload rejected without a job');
  assert.deepEqual(errors,[]);assert.deepEqual(remote,[]);
  const report={status:'passed',created_at:new Date().toISOString(),checks,console_errors:errors,external_http_requests:remote};await writeFile(join(reportDirectory,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
} finally {await browser.close();child.kill('SIGINT');await new Promise(resolve=>{child.once('exit',resolve);setTimeout(()=>{child.kill('SIGKILL');resolve();},10000).unref();});}
