/* All document content is inserted with textContent. No remote services or assets. */
const $ = id => document.getElementById(id);
const state = {session: null, projectId: null, projects: [], uploading: false, documents: [], selected: null, job: null, summary: null, method: 'normalized', page: 1, pageData: null, structure: null, chunks: [], chunkOffset: 0, chunkTotal: 0, chunkPosition: 0, chunk: null, pdf: false, viewSequence: 0, query: '', listOffset: 0, limit: 50, refreshing: false};
const descriptions = {baseline: 'Original sorted text. Columns can interleave.', layout: 'Native blocks in geometric reading order.', normalized: 'Conservative edits, with original source ranges.', ocr: 'Local OCR on pages without native text.', structure: 'Paragraphs, outline headings, tables and image references.', chunks: 'Bounded text with section boundaries and source references.'};
const labels = {queued: 'Queued', starting: 'Starting', parsing: 'Parsing', ocr: 'Recognizing scan', saving: 'Saving results', chunking: 'Chunking', ready: 'Ready', failed: 'Failed'};
function node(tag, text, className) {const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (className) element.className = className; return element;}
function bytes(value) {if (value < 1024) return value + ' B'; if (value < 1024 ** 2) return (value / 1024).toFixed(1) + ' KB'; return (value / 1024 ** 2).toFixed(1) + ' MB';}
function chars(text) {return Array.from(text || '');}
function toast(message) {$('toast').textContent = message; $('toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').hidden = true, 6000);}
async function api(path, options = {}) {options.headers = {...options.headers}; if (state.projectId) options.headers['X-Aegis-Project'] = state.projectId; if (options.method && options.method !== 'GET') options.headers['X-Aegis-Token'] = state.session.token; const response = await fetch(path, options); const data = await response.json(); if (response.status === 401) location.replace('/login'); if (!response.ok) {const error = Error(data.error || 'Request failed'); error.status = response.status; throw error;} return data;}
function post(path) {return api(path, {method: 'POST', headers: {'X-Aegis-Token': state.session.token}});}
function config() {const size = Number($('chunk-size').value), overlap = Number($('chunk-overlap').value); if (!Number.isInteger(size) || size < 32 || size > 16000 || !Number.isInteger(overlap) || overlap < 0 || overlap >= size) throw Error('Use a chunk size from 32–16,000 characters and a smaller overlap.'); return {max_chars: size, overlap_chars: overlap};}
function renderLibrary() {
  const focus = document.activeElement?.dataset.document;
  $('library').replaceChildren(); $('library-count').textContent = state.total || 0;
  if (!state.documents.length) $('library').append(node('div', state.query ? 'No matching documents.' : 'Your documents will appear here.', 'library-empty'));
  for (const document of state.documents) {
    const button = node('button', undefined, 'document-card' + (document.id === state.selected ? ' active' : '')); button.dataset.document = document.id; button.title = document.name;
    button.append(node('span', document.format.toUpperCase(), 'file-icon'));
    const text = node('span', undefined, 'file-text'); text.append(node('span', document.name, 'file-name'));
    const sub = node('span', undefined, 'file-sub'); sub.append(node('span', bytes(document.size_bytes)), node('span', '·'), node('span', '', 'status-dot ' + document.status), node('span', labels[document.stage] || document.status)); text.append(sub); button.append(text);
    button.onclick = () => selectDocument(document.id); $('library').append(button);
  }
  $('more-documents').hidden = state.listOffset + state.documents.length >= (state.total || 0);
  $('previous-documents').hidden = state.listOffset === 0;
  if (focus) [...$('library').children].find(item => item.dataset.document === focus)?.focus({preventScroll: true});
}
async function refreshEvidenceStatus(project) {
  const status=await api('/api/search/status');if(project!==state.projectId)return;
  const selected=$('evidence-document').value;
  $('evidence-document').replaceChildren(node('option','All documents'));
  $('evidence-document').firstChild.value='';
  for(const item of status.sources){const option=node('option',item.name);option.value=item.id;$('evidence-document').append(option);}
  if([...$('evidence-document').options].some(item=>item.value===selected))$('evidence-document').value=selected;
  const scope=$('research-document'),oldScope=scope.value;scope.replaceChildren(node('option','All project documents'));scope.firstChild.value='';for(const item of status.sources){if(item.status!=='ready')continue;const option=node('option',item.name);option.value=item.id;scope.append(option);}if([...scope.options].some(item=>item.value===oldScope))scope.value=oldScope;
  for(const option of $('evidence-mode').options)option.disabled=option.value!=='sparse'&&!status.dense_available;
  if(!status.dense_available){$('evidence-mode').value='sparse';if(!$('evidence-results').childNodes.length)$('evidence-status').textContent=status.model_error;}
}
async function refresh() {
  if (state.refreshing || !state.projectId) return; const project = state.projectId;
  state.refreshing = true;
  try {
    const data = await api(`/api/documents?offset=${state.listOffset}&limit=${state.limit}&q=${encodeURIComponent(state.query)}`); if (project !== state.projectId) return; state.documents = data.documents; state.total = data.total; renderLibrary();
    await refreshEvidenceStatus(project); if(project!==state.projectId)return;
    if (!state.selected && !state.query && state.documents.length) await selectDocument(state.documents[0].id);
    else if (state.selected) {
      const job = state.documents.find(item => item.id === state.selected) || await api('/api/documents/' + state.selected);
      const previous = state.job?.status; state.job = job; renderJob();
      if (job.status === 'ready' && previous !== 'ready') await loadReady();
    }
  } catch (error) {if (error.status === 404) {state.selected = null; state.job = null; renderJob();} toast(error.message);} finally {state.refreshing = false;}
}
async function selectDocument(id) {
  ++state.viewSequence; state.selected = id; state.job = state.documents.find(item => item.id === id) || await api('/api/documents/' + id);
  state.summary = null; state.pageData = null; state.structure = null; state.page = 1; state.pdf = false; state.chunk = null;
  state.method = state.job.format === 'docx' ? 'structure' : 'normalized'; renderLibrary(); renderJob();
  if (state.job.status === 'ready') await loadReady();
}
function renderJob() {
  const job = state.job; $('welcome').hidden = !!job; $('document-workspace').hidden = !job;
  if (!job) {
    ++state.viewSequence;
    $('preview').removeAttribute('src'); $('pdf-frame').removeAttribute('src');
    $('docx-preview').replaceChildren(); resetOutput();
    return;
  }
  $('document-name').textContent = job.name; $('document-meta').replaceChildren(node('span', job.format.toUpperCase()), node('span', bytes(job.size_bytes)));
  if (job.pages) $('document-meta').append(node('span', job.pages + ' pages'));
  if (job.status === 'ready') $('document-meta').append(node('span', job.chunks + ' chunks'));
  $('document-meta').append(node('span', job.role + (job.revision ? ' · ' + job.revision : ''))); $('document-meta').append(node('span', labels[job.stage] || job.status, 'state ' + job.status));
  $('original').href = `/api/documents/${job.id}/original`; $('retry').hidden = job.status !== 'failed'; $('remove').disabled = false;
  const busy = ['queued', 'processing'].includes(job.status); $('progress').hidden = !busy; $('job-error').hidden = job.status !== 'failed'; $('ready').hidden = job.status !== 'ready' || !state.summary;
  if (busy) { $('progress-label').textContent = labels[job.stage] || 'Processing'; $('progress-count').textContent = job.total ? `${job.done} / ${job.total}` : job.done ? `${job.done} chunks` : job.status === 'queued' ? 'Waiting for a worker' : ''; $('progress-bar').classList.toggle('indeterminate', !job.total); $('progress-bar').style.width = (job.total ? Math.max(2, job.done / job.total * 100) : 10) + '%'; }
  if (job.status === 'failed') $('job-error').textContent = job.error || 'Document processing failed.';
}
async function loadReady() {
  const id = state.selected, sequence = ++state.viewSequence;
  try {const summary = await api(`/api/documents/${id}/summary`); if (sequence !== state.viewSequence || id !== state.selected) return; state.summary = summary;
    document.querySelectorAll('[data-method]').forEach(button => button.hidden = !summary.methods.includes(button.dataset.method));
    $('page-controls').hidden = state.job.format !== 'pdf'; $('source-heading').textContent = state.job.format === 'pdf' ? 'Source page' : 'Document structure';
    if (state.job.format === 'docx') {const structure = await api(`/api/documents/${id}/structure`); if (sequence !== state.viewSequence) return; state.structure = structure; renderStructure();}
    await loadView();
    if (sequence <= state.viewSequence && id === state.selected) $('ready').hidden = false;
  } catch (error) {toast(error.message);}
}
function resetOutput() {$('output').replaceChildren(); $('overlay').replaceChildren(); $('provenance').replaceChildren(); $('note').hidden = true; $('note').textContent = '';}
async function loadView() {
  if (!state.summary) return;
  const id = state.selected, sequence = ++state.viewSequence; resetOutput(); $('output').append(node('div', 'Loading…', 'empty-output'));
  try {
    if (state.job.format === 'pdf') {const page = await api(`/api/documents/${id}/pages/${state.page}`); if (sequence !== state.viewSequence) return; state.pageData = page; $('page-number').value = state.page; $('page-number').max = state.summary.pages; $('page-total').textContent = '/ ' + state.summary.pages; $('page-previous').disabled = state.page === 1; $('page-next').disabled = state.page === state.summary.pages;
      $('preview').src = `/api/documents/${id}/pages/${state.page}/image`; $('preview').alt = `${state.job.name}, page ${state.page}`; $('page-image').hidden = state.pdf; $('pdf-frame').hidden = !state.pdf; $('docx-preview').hidden = true;
      if (state.pdf) $('pdf-frame').src = `/api/documents/${id}/original#page=${state.page}&view=FitH`; else $('pdf-frame').removeAttribute('src');
    } else {$('page-image').hidden = true; $('pdf-frame').hidden = true; $('docx-preview').hidden = false;}
    if (state.method === 'chunks') {state.chunkOffset = 0; await loadChunkList(sequence);} else renderMethod();
  } catch (error) {if (sequence === state.viewSequence) {resetOutput(); $('output').append(node('div', error.message, 'empty-output'));}}
}
function showNote(warnings) {const values = [...new Set(warnings.filter(Boolean))]; $('note').textContent = values.join(' · '); $('note').hidden = !values.length;}
function detail(label, value) {const row = node('p'); row.append(node('strong', label + ' · '), document.createTextNode(String(value))); $('provenance').append(row);}
function trace(sources) {
  $('overlay').replaceChildren(); document.querySelectorAll('.source-active').forEach(item => item.classList.remove('source-active'));
  if (state.job.format === 'docx') {for (const source of sources) {const element = [...$('docx-preview').querySelectorAll('[data-source]')].find(item => item.dataset.source === source.xml_path); if (element) {element.classList.add('source-active'); element.scrollIntoView({block: 'nearest'});}} return;}
  const page = state.pageData; if (!page) return;
  const matrix = page.rotation_matrix;
  for (const source of sources) {if (source.page && source.page !== state.page) continue; const box = source.bbox; if (!box) continue;
    const corners = [[box[0], box[1]], [box[2], box[1]], [box[0], box[3]], [box[2], box[3]]].map(([x, y]) => [x * matrix[0] + y * matrix[2] + matrix[4], x * matrix[1] + y * matrix[3] + matrix[5]]);
    const xs = corners.map(point => point[0]), ys = corners.map(point => point[1]); const left = Math.min(...xs), top = Math.min(...ys), width = Math.max(...xs) - left, height = Math.max(...ys) - top;
    const element = node('div', undefined, 'source-box'); Object.assign(element.style, {left: left / page.display_width * 100 + '%', top: top / page.display_height * 100 + '%', width: width / page.display_width * 100 + '%', height: height / page.display_height * 100 + '%'}); $('overlay').append(element);
  }
}
function block(text, label, sources, extra='') {
  if (!text) return;
  const element = node('div', undefined, 'text-block'); element.tabIndex = 0;
  const title = node('div', undefined, 'block-label'); title.append(node('span', label), node('span', extra)); element.append(title, node('pre', text));
  for (const event of ['mouseenter', 'focus', 'click']) element.addEventListener(event, () => trace(sources)); $('output').append(element);
}
function renderMethod() {
  resetOutput(); $('chunk-controls').hidden = state.method !== 'chunks'; $('output-count').hidden = state.method === 'chunks';
  document.querySelectorAll('[data-method]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.method === state.method)));
  $('method-description').textContent = descriptions[state.method]; $('method-stat').textContent = state.method === 'chunks' ? `${state.summary.chunk_config.max_chars} chars · ${state.summary.chunk_config.overlap_chars} max overlap` : '';
  $('output-heading').textContent = state.method === 'chunks' ? 'Chunk text' : state.method === 'structure' ? 'Extracted structure' : 'Extracted text';
  detail('Document SHA-256', state.summary.document_id); detail('Method', state.method); detail('Processing', `${state.job.elapsed_seconds}s · ${state.summary.coverage.status} chunk coverage`);
  const page = state.pageData;
  if (state.method === 'structure') {for (const item of state.structure.blocks) {if (item.kind === 'paragraph') block(item.text, item.heading_level ? `Heading ${item.heading_level}` : 'Paragraph', [{xml_path: item.source}]); else if (item.kind === 'table') {const values=[]; function visit(items) {for (const item of items) {if (item.kind==='paragraph' && item.text) values.push(item); visit(item.children);}} visit(item.children); block(values.map(item=>item.text).join('\n'), 'Table', values.map(item=>({xml_path:item.source})));}} $('output-count').textContent = state.structure.blocks.length + ' blocks'; showNote(state.summary.warnings);}
  else if (state.method === 'baseline') {$('output').append(node('pre', page.text || 'No native text. Try OCR.')); $('output-count').textContent = chars(page.text).length + ' chars'; showNote(page.warnings || []);}
  else if (state.method === 'layout') {const byId = new Map(page.layout.blocks.map(item=>[item.block_id,item])); page.layout.reading_order.forEach((id, index)=>{const item=byId.get(id); block(item.lines.map(line=>line.spans.map(span=>span.text).join('')).join('\n'), 'Block '+(index+1), [{page: state.page,bbox:item.bbox}]);}); $('output-count').textContent=page.layout.blocks.length+' blocks'; showNote(page.layout.warnings || []);}
  else if (state.method === 'normalized') {const byId = new Map(page.layout.blocks.map(item=>[item.block_id,item])); page.normalized.blocks.forEach((item,index)=>{const sources=item.mappings.flatMap(mapping=>mapping.sources).map(source=>({page:state.page,bbox:byId.get(source.block_id)?.lines[source.line_index]?.bbox})).filter(source=>source.bbox); block(item.text,'Block '+(index+1),sources,item.changes.length+' edits');}); $('output-count').textContent=chars(page.normalized_text).length+' chars'; showNote([...page.warnings,...page.normalized.warnings]); detail('Edits', JSON.stringify(page.normalized.blocks.flatMap(item=>item.changes)));}
  else if (state.method === 'ocr') {if (!page.ocr) {$('output').append(node('div', page.text ? 'Native text present. OCR was skipped on this page.' : 'No OCR text available.', 'empty-output')); $('output-count').textContent='Not run'; showNote(page.warnings || []);} else {const lines=new Map(); for(const word of page.ocr.words) {const key=`${word.block}:${word.paragraph}:${word.line}`; if(!lines.has(key)) lines.set(key,[]); lines.get(key).push(word);} let index=0; for(const words of lines.values()) {const confidence=words.reduce((sum,word)=>sum+word.confidence,0)/words.length; block(words.map(word=>word.text).join(' '),'Line '+(++index),words.map(word=>({page:state.page,bbox:word.bbox})),`Confidence ${confidence.toFixed(1)}`);} $('output-count').textContent=page.ocr.words.length+' words'; showNote(page.ocr.warnings); detail('Engine', page.ocr.engine.version); detail('Orientation',page.ocr.rotation_correction+'° correction');}}
  if (!$('output').childNodes.length) $('output').append(node('div','No text in this view.', 'empty-output'));
}
async function loadChunkList(sequence = ++state.viewSequence) {
  const filter = state.job.format === 'pdf' ? `&page=${state.page}` : '';
  const focus = state.focusChunk === undefined ? '' : `&focus=${state.focusChunk}`;
  const data = await api(`/api/documents/${state.selected}/chunks?offset=${state.chunkOffset}&limit=50${filter}${focus}`); if(sequence!==state.viewSequence)return;
  state.chunkOffset=data.offset; state.chunks=data.chunks; state.chunkTotal=data.total; state.chunkPosition=0;
  if(state.focusChunk !== undefined){state.chunkPosition=Math.max(0,state.chunks.findIndex(chunk=>chunk.index===state.focusChunk));delete state.focusChunk;} $('chunk-select').replaceChildren();
  state.chunks.forEach((chunk,index)=>{const option=node('option','Chunk '+(chunk.index+1)); option.value=index; $('chunk-select').append(option);});
  if(!state.chunks.length) {renderMethod(); $('output').replaceChildren(node('div','No text chunks on this page.', 'empty-output')); $('chunk-previous').disabled=true; $('chunk-next').disabled=true; return;}
  await loadChunk(sequence);
}
async function loadChunk(sequence=++state.viewSequence) {
  const selected=state.chunks[state.chunkPosition]; if(!selected)return;
  const chunk=await api(`/api/documents/${state.selected}/chunks/${selected.index}`); if(sequence!==state.viewSequence)return; state.chunk=chunk; renderMethod(); $('output').replaceChildren(); $('chunk-select').value=state.chunkPosition;
  $('chunk-previous').disabled=state.chunkOffset+state.chunkPosition===0; $('chunk-next').disabled=state.chunkOffset+state.chunkPosition>=state.chunkTotal-1;
  const overlap=chunk.fragments.reduce((sum,item)=>sum+item.overlap_prefix_chars,0); const title=node('div',undefined,'block-label');title.append(node('span',chars(chunk.text).length+' chars'),node('span',overlap+' overlap'));$('output').append(title);
  if(chunk.headings.length)$('output').append(node('div',chunk.headings.map(item=>item.text).join(' / '),'chunk-heading'));
  const pre=node('pre'), text=chars(chunk.text);let cursor=0;
  for(const part of chunk.fragments.filter(item=>item.overlap_prefix_chars)){pre.append(document.createTextNode(text.slice(cursor,part.output_start).join('')));pre.append(node('mark',text.slice(part.output_start,part.output_start+part.overlap_prefix_chars).join('')));cursor=part.output_start+part.overlap_prefix_chars;}
  pre.append(document.createTextNode(text.slice(cursor).join('')));$('output').append(pre);trace(chunk.mappings.flatMap(item=>item.sources)); showNote(chunk.warnings);detail('Chunk SHA-256',chunk.chunk_id);detail('Algorithm',chunk.algorithm);
  const mappings=node('pre',JSON.stringify(chunk.mappings,null,2));$('provenance').append(mappings);
}
function renderStructure() {
  $('docx-preview').replaceChildren();
  function build(item) {if(item.kind==='paragraph'){const element=node('div',item.text || (item.image_ids.length?'[Embedded image]':''),'docx-paragraph');element.dataset.source=item.source;if(item.heading_level)element.dataset.heading=item.heading_level;return element;}
    if(item.kind==='table'){const table=node('table',undefined,'docx-table');for(const row of item.children){const tr=node('tr');for(const cell of row.children){const td=node('td');td.colSpan=Math.max(1,Math.min(100,cell.column_span));for(const child of cell.children)td.append(build(child));tr.append(td);}table.append(tr);}return table;}return node('div');}
  state.structure.blocks.forEach(item=>$('docx-preview').append(build(item)));
}
async function changePage(number) {number=Number(number);if(!Number.isInteger(number)||number<1||number>state.summary.pages){$('page-number').value=state.page;return;}state.page=number;await loadView();$('source-scroll').scrollTop=0;}
function uploadOne(file, options) {return new Promise((resolve,reject)=>{if(!/\.(pdf|docx)$/i.test(file.name))return reject(Error(file.name+': PDF or DOCX only.'));if(file.size>state.session.max_file_bytes)return reject(Error(file.name+': exceeds '+bytes(state.session.max_file_bytes)+'.'));const card=node('div',file.name,'upload-card');card.append(node('span',bytes(file.size)+' · Uploading'));const track=node('div',undefined,'upload-track'),fill=node('div');track.append(fill);card.append(track);$('uploads').append(card);const request=new XMLHttpRequest();request.open('POST','/api/documents');request.setRequestHeader('Content-Type','application/octet-stream');request.setRequestHeader('X-Aegis-Token',state.session.token);request.setRequestHeader('X-Aegis-Project',state.projectId);request.setRequestHeader('X-Aegis-Revision', encodeURIComponent($('revision').value));request.setRequestHeader('X-Aegis-Role', $('document-role').value);request.setRequestHeader('X-Filename',encodeURIComponent(file.name));request.setRequestHeader('X-Aegis-Config',JSON.stringify(options));request.upload.onprogress=event=>{if(event.lengthComputable)fill.style.width=event.loaded/event.total*100+'%';};request.onload=()=>{card.remove();let data;try{data=JSON.parse(request.responseText);}catch{return reject(Error('Upload failed.'));}if(request.status===401)location.replace('/login');if(request.status>=400)return reject(Error(data.error));resolve(data);};request.onerror=()=>{card.remove();reject(Error('Upload connection failed.'));};request.send(file);});}
async function uploadFiles(files) {if (!state.projectId) {openProjectDialog();return;} if (state.uploading) {toast('Wait for the current upload batch to finish.');return;} let options;try{options=config();}catch(error){toast(error.message);return;}state.uploading=true;updateProjectControls();state.listOffset=0;const queue=Array.from(files);let first=null;async function next(){while(queue.length){const file=queue.shift();try{const job=await uploadOne(file,options);if(!first){first=job.id;await selectDocument(job.id);}await refresh();}catch(error){toast(error.message);}}}try {await Promise.all([next(),next()]);} finally {state.uploading=false;updateProjectControls();$('file-input').value='';}}
for(const id of ['add','add-small','choose'])$(id).onclick=()=>state.projectId ? $('file-input').click() : openProjectDialog();$('file-input').onchange=event=>uploadFiles(event.target.files);
$('settings-button').onclick=()=>{$('settings').hidden=!$('settings').hidden;$('settings-button').setAttribute('aria-expanded',String(!$('settings').hidden));};
$('samples').onclick=async()=>{const button=$('samples');button.disabled=true;try{const result=await post('/api/samples');await selectDocument(result.documents[0].id);await refresh();}catch(error){toast(error.message);}finally{button.disabled=false;}};
$('retry').onclick=async()=>{try{state.job=await post(`/api/documents/${state.selected}/retry`);renderJob();await refresh();}catch(error){toast(error.message);}};
$('remove').onclick=async()=>{if(!confirm('Permanently delete this document, its source file, extracted evidence and indexes? Active processing will stop.'))return;try{await post(`/api/documents/${state.selected}/remove`);state.selected=null;state.job=null;state.summary=null;renderJob();await refresh();}catch(error){toast(error.message);}};
let searchTimer;$('search').oninput=event=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.query=event.target.value;state.listOffset=0;refresh();},180);};$('more-documents').onclick=()=>{state.listOffset+=50;refresh();};$('previous-documents').onclick=()=>{state.listOffset=Math.max(0,state.listOffset-50);refresh();};
for(const button of document.querySelectorAll('[data-method]'))button.onclick=()=>{++state.viewSequence;state.method=button.dataset.method;state.method==='chunks'?loadView():renderMethod();};
$('page-previous').onclick=()=>changePage(state.page-1);$('page-next').onclick=()=>changePage(state.page+1);$('page-number').onchange=event=>changePage(event.target.value);
$('pdf-toggle').onclick=()=>{state.pdf=!state.pdf;$('pdf-toggle').setAttribute('aria-pressed',String(state.pdf));loadView();};
$('chunk-select').onchange=event=>{state.chunkPosition=Number(event.target.value);loadChunk();};
$('chunk-previous').onclick=async()=>{if(state.chunkPosition>0){state.chunkPosition--;await loadChunk();}else if(state.chunkOffset>0){state.chunkOffset-=50;await loadChunkList();state.chunkPosition=state.chunks.length-1;await loadChunk();}};
$('chunk-next').onclick=async()=>{if(state.chunkPosition<state.chunks.length-1){state.chunkPosition++;await loadChunk();}else if(state.chunkOffset+state.chunks.length<state.chunkTotal){state.chunkOffset+=50;await loadChunkList();}};
let dragDepth=0;document.addEventListener('dragenter',event=>{if(event.dataTransfer.types.includes('Files')){event.preventDefault();dragDepth++;$('drop-overlay').hidden=false;}});document.addEventListener('dragleave',()=>{dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)$('drop-overlay').hidden=true;});document.addEventListener('dragover',event=>event.preventDefault());document.addEventListener('drop',event=>{event.preventDefault();dragDepth=0;$('drop-overlay').hidden=true;if(event.dataTransfer.files.length)uploadFiles(event.dataTransfer.files);});
$('preview').onerror=()=>toast('Page preview unavailable. Use Open original to inspect the PDF.');
function updateProjectControls() {
  for (const id of ['project-select','new-project','edit-project','delete-project']) $(id).disabled = !state.session || state.uploading || (['edit-project','delete-project'].includes(id) && !state.projectId);
  $('settings-button').disabled = !state.projectId;
  $('evidence-search').hidden = !state.projectId; $('research-panel').hidden = !state.projectId;
}
function renderProjects() {
  $('project-select').replaceChildren();
  if (!state.projects.length) {const option=node('option','Create your first project');option.value='';$('project-select').append(option);}
  for (const project of state.projects) {const option=node('option',project.name);option.value=project.id;$('project-select').append(option);}
  $('project-select').value=state.projectId || '';
  const project=state.projects.find(p=>p.id===state.projectId);
  $('project-title').textContent=project?.name || 'Your projects';
  $('welcome').querySelector('h1').textContent=project ? 'Add your documents.' : 'Create your first project.';
  $('choose').textContent=project ? 'Choose files' : 'Create project';
  updateProjectControls();
}
async function chooseProject(identity) {
  if($('evidence-drawer').open)$('evidence-drawer').close();$('evidence-text').replaceChildren();
  state.historyOffset=0;state.researchId=null;state.researchRun=null;state.chatRuns={};state.requestStarted=null;state.timerRun=null;state.requestFinished=null;$('research-timing').textContent='';$('research-panel').classList.remove('has-conversation');state.researchBusy=false;state.researchSequence=(state.researchSequence||0)+1;
  $('research-answer').replaceChildren();$('research-trace').replaceChildren();$('research-status').textContent='';
  for(const id of ['research-cancel','research-retry','research-followup','research-report'])$(id).hidden=true;
  $('evidence-results').replaceChildren(); $('evidence-status').textContent='Search the evidence in this project.';

  if (state.uploading) return;
  ++state.viewSequence;state.projectId=identity || null;state.selected=null;state.job=null;state.summary=null;state.documents=[];state.total=0;state.listOffset=0;state.query='';$('search').value='';
  if (state.projectId) localStorage.setItem('aegis.project:'+state.session.user.id,state.projectId);
  renderProjects();renderLibrary();renderJob();await refresh();await refreshResearch();
}
let editingProject=null;
function openProjectDialog(project=null) {
  editingProject=project;$('project-dialog-title').textContent=project ? 'Edit project' : 'New project';$('project-name').value=project?.name || '';$('project-description').value=project?.description || '';$('project-error').hidden=true;$('project-dialog').showModal();
}
$('new-project').onclick=()=>openProjectDialog();$('project-cancel').onclick=()=>$('project-dialog').close();
$('edit-project').onclick=()=>openProjectDialog(state.projects.find(p=>p.id===state.projectId));
$('project-select').onchange=event=>chooseProject(event.target.value);
$('project-form').onsubmit=async event=>{event.preventDefault();$('project-save').disabled=true;try{const project=await api(editingProject ? '/api/projects/'+editingProject.id : '/api/projects',{method:editingProject ? 'PATCH' : 'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('project-name').value,description:$('project-description').value})});state.projects=(await api('/api/projects')).projects;$('project-dialog').close();await chooseProject(project.id);}catch(error){$('project-error').textContent=error.message;$('project-error').hidden=false;}finally{$('project-save').disabled=false;}};
$('delete-project').onclick=async()=>{const project=state.projects.find(p=>p.id===state.projectId);if(!project || !confirm('Permanently delete '+project.name+' and all its documents and extracted evidence?'))return;try{await api('/api/projects/'+project.id,{method:'DELETE'});state.projects=(await api('/api/projects')).projects;await chooseProject(state.projects[0]?.id);}catch(error){toast(error.message);}};
$('document-settings').onclick=()=>{$('edit-revision').value=state.job.revision;$('edit-role').value=state.job.role;$('revision-error').hidden=true;$('revision-dialog').showModal();};
$('revision-cancel').onclick=()=>$('revision-dialog').close();
$('revision-form').onsubmit=async event=>{event.preventDefault();$('revision-save').disabled=true;try{state.job=await api('/api/documents/'+state.selected,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:$('edit-revision').value,role:$('edit-role').value})});$('revision-dialog').close();renderJob();}catch(error){$('revision-error').textContent=error.message;$('revision-error').hidden=false;}finally{$('revision-save').disabled=false;}};
$('evidence-form').onsubmit=async event=>{
  event.preventDefault(); const project=state.projectId; $('evidence-submit').disabled=true;
  $('evidence-results').replaceChildren(); $('evidence-status').textContent='Retrieving project evidence…';
  const params=new URLSearchParams({q:$('evidence-query').value,mode:$('evidence-mode').value});
  for(const [field,id] of [['document_id','document'],['revision','revision'],['role','role'],['section','section'],['kind','kind'],['format','format']]){const value=$('evidence-'+id).value;if(value)params.set(field,value);}
  try {
    const report=await api('/api/search?'+params); if(project!==state.projectId)return;
    const status=await api('/api/search/status'); if(project!==state.projectId)return;
    const chunks=status.documents.reduce((sum,row)=>sum+row.chunks,0), embedded=status.documents.reduce((sum,row)=>sum+row.embedded,0);
    $('evidence-status').textContent=report.results.length+' results · '+report.latency_ms+' ms · '+report.context_chars+' context characters · '+embedded+'/'+chunks+' chunks embedded'+(status.index_error?' · Indexing needs attention':'')+(status.model_error?' · '+status.model_error:'')+(report.results.length?'':' · No matching indexed evidence; new documents may still be indexing.');
    for(const hit of report.results){
      const article=node('article',undefined,'evidence-result');article.append(node('h3',hit.name));
      article.append(node('div',hit.role+(hit.revision?' · '+hit.revision:'')+' · '+(hit.pages.length?'Page '+hit.pages.join(', '):'DOCX')+' · '+hit.method,'evidence-meta'));
      if(hit.chunk.headings?.length)article.append(node('div',hit.chunk.headings.map(h=>h.text).join(' › '),'evidence-meta'));
      article.append(node('p',hit.chunk.text));
      const link=node('a','Inspect exact source ↗','subtle');link.href=hit.source_url;article.append(link);
      if(hit.neighbors.length){const details=node('details');details.append(node('summary','Surrounding evidence'));for(const chunk of hit.neighbors)details.append(node('p',chunk.text));article.append(details);}
      if(hit.chunk.warnings?.length)article.append(node('p',hit.chunk.warnings.join(' · '),'muted'));
      $('evidence-results').append(article);
    }
  }catch(error){if(project===state.projectId)$('evidence-status').textContent=error.message;}finally{$('evidence-submit').disabled=false;}
};
async function refreshResearch(){
  if(!state.projectId||state.researchBusy)return;state.researchBusy=true;const project=state.projectId,sequence=state.researchSequence;
  try{
    const history=await api('/api/research?offset='+(state.historyOffset||0));if(project!==state.projectId||sequence!==state.researchSequence)return;
    $('research-newer').hidden=!(state.historyOffset||0);$('research-older').hidden=history.runs.length<20;
    $('research-history').replaceChildren(node('option','Choose a saved run'));$('research-history').firstChild.value='';
    $('chat-history').replaceChildren(node('p','Recent chats','history-label'));for(const entry of history.runs){const button=node('button',entry.question,'history-item');button.dataset.run=entry.id;button.title=entry.question;button.onclick=()=>{state.researchId=entry.id;state.requestStarted=null;state.requestFinished=null;state.timerRun=null;state.researchSequence=(state.researchSequence||0)+1;showView('chat');refreshResearch();};$('chat-history').append(button);}
    for(const run of history.runs){const option=node('option',run.status+' · '+run.question.slice(0,90));option.value=run.id;$('research-history').append(option);}
    if(state.researchId){$('research-history').value=state.researchId;const run=await api('/api/research/'+state.researchId);if(project!==state.projectId||sequence!==state.researchSequence)return;state.chatRuns=state.chatRuns||{};let ancestor=run;for(let depth=0;ancestor.parent_id&&depth<16;depth++){const parent=state.chatRuns[ancestor.parent_id]||await api('/api/research/'+ancestor.parent_id);state.chatRuns[parent.id]=parent;ancestor=parent;}renderResearch(run);}
  }catch(error){if(project===state.projectId){$('research-status').textContent=error.message;if(error.status===404){state.researchId=null;state.researchRun=null;$('research-answer').replaceChildren();$('research-trace').replaceChildren();for(const id of ['research-cancel','research-retry','research-followup','research-report'])$(id).hidden=true;}}}finally{state.researchBusy=false;}
}
async function showEvidence(run,id,quote){
  try{const source=await api('/api/research/'+run.id+'/evidence/'+id);$('evidence-title').textContent=source.id+' · '+source.name;$('evidence-metadata').textContent=source.role+' · '+source.revision+' · '+(source.pages.length?'Page '+source.pages.join(', '):'DOCX')+' · SHA-256 '+source.document_sha256;$('evidence-text').replaceChildren();const at=source.text.indexOf(quote||'');if(quote&&at>=0){$('evidence-text').append(document.createTextNode(source.text.slice(0,at)),node('mark',quote),document.createTextNode(source.text.slice(at+quote.length)));}else $('evidence-text').textContent=source.text;$('evidence-source').href=source.source_url;$('evidence-mappings').textContent=JSON.stringify({excerpt_range:[source.chunk_start,source.chunk_end],mappings:source.mappings,warnings:source.warnings},null,2);$('evidence-drawer').showModal();}catch(error){toast(error.message);}
}
function renderResearch(run){
  state.researchRun=run;if(state.timerRun!==run.id){state.timerRun=run.id;if(!state.requestStarted){state.timerHistorical=true;const stop=['completed','failed','cancelled'].includes(run.status)?run.updated_at*1000:Date.now();state.requestStarted=performance.now()-Math.max(0,stop-run.created_at*1000);}state.requestFinished=null;}if(['completed','failed','cancelled'].includes(run.status)&&state.requestFinished===null){state.requestFinished=performance.now();}updateResearchTimer();$('research-panel').classList.add('has-conversation');$('research-status').textContent=run.status==='completed'?'':run.status==='running'?({retrieving:'Finding relevant passages…',planning:'Planning the investigation…',evidence_saved:'Preparing source evidence…',generating:'Writing your answer…',checking_support:'Checking the answer against its sources…'}[run.stage]||'Reading your documents…'):run.status==='queued'?'Waiting to start…':run.error||run.status;
  const terminal=['completed','failed','cancelled'].includes(run.status);$('research-cancel').hidden=terminal;$('research-retry').hidden=!['failed','cancelled'].includes(run.status);$('research-followup').hidden=run.status!=='completed';$('research-report').hidden=run.status!=='completed';$('research-report').href='/api/research/'+run.id+'/report';
  state.chatRuns=state.chatRuns||{};state.chatRuns[run.id]=run;
  $('research-answer').replaceChildren();$('research-trace').replaceChildren();
  const preceding=[];let previous=state.chatRuns[run.parent_id];while(previous&&preceding.length<16){preceding.unshift(previous);previous=state.chatRuns[previous.parent_id];}
  for(const earlier of preceding){$('research-answer').append(node('p',earlier.question,'chat-question'));for(const claim of earlier.result?.claims||[]){const item=node('article',undefined,'research-claim');item.append(node('p',claim.text));for(const citation of claim.citations){const button=node('button',citation.evidence_id+' · Source');button.onclick=()=>showEvidence(earlier,citation.evidence_id,citation.quote);item.append(button);}$('research-answer').append(item);}}
  $('research-trace').append(node('div',JSON.stringify(run.config,null,2),'research-trace-row'));
  for(const step of run.trace)$('research-trace').append(node('div',JSON.stringify(step,null,2),'research-trace-row'));
  $('research-answer').append(node('p',run.question,'chat-question'));
  if(!run.result)return;
  if(!run.result.claims.length)$('research-answer').append(node('p','I could not verify an answer from the available passages. Try a more specific question or check your documents.','research-unresolved'));
  function refs(target,citations){const group=node('div',undefined,'research-citations');for(const citation of citations){const button=node('button','Source '+Number(citation.evidence_id.slice(1)));button.onclick=()=>showEvidence(run,citation.evidence_id,citation.quote);button.title=citation.quote;group.append(button);}target.append(group);}
  for(const claim of run.result.claims){const item=node('article',undefined,'research-claim');item.append(node('p',claim.text));refs(item,claim.citations);const check=node('details',undefined,'answer-details');check.append(node('summary','Verification'),node('p',claim.support.replaceAll('_',' ')+(claim.support_reason?' · '+claim.support_reason:'')));item.append(check);$('research-answer').append(item);}
  for(const conflict of run.result.potential_conflicts){const item=node('article',undefined,'research-claim');item.append(node('h3','Potential conflicting passages — needs human review'));item.append(node('p',conflict.description));refs(item,conflict.citations);$('research-answer').append(item);}
  const details=node('details',undefined,'answer-details');details.append(node('summary','Answer checks and limits'));for(const reason of [...run.result.unresolved,...(run.result.limitations||[])])details.append(node('p',reason,'research-unresolved'));$('research-answer').append(details);
}
async function startResearch(parent=null){
  const project=state.projectId;state.requestStarted=performance.now();state.timerHistorical=false;state.timerRun=null;state.requestFinished=null;$('research-timing').textContent='0.0 s · Starting';$('research-submit').disabled=true;$('research-status').textContent='Queueing local research…';
  try{const run=await api('/api/research',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:$('research-question').value,mode:$('research-mode').value,retrieval:$('research-retrieval').value,parent_id:parent,document_id:$('research-document').value||null})});if(project!==state.projectId)return;state.historyOffset=0;state.researchId=run.id;state.researchSequence=(state.researchSequence||0)+1;renderResearch(run);$('research-question').value='';await refreshResearch();}catch(error){if(project===state.projectId){state.requestFinished=performance.now();updateResearchTimer();$('research-status').textContent=error.message;}}finally{$('research-submit').disabled=false;}
}
$('research-form').onsubmit=event=>{event.preventDefault();startResearch(state.researchRun?.status==='completed'?state.researchId:null);};
$('research-followup').onclick=()=>{if(!$('research-question').reportValidity())return;startResearch(state.researchId);};
$('research-history').onchange=async event=>{state.researchId=event.target.value||null;state.requestStarted=null;state.timerRun=null;state.requestFinished=null;state.researchSequence=(state.researchSequence||0)+1;if(state.researchId)await refreshResearch();else{$('research-answer').replaceChildren();$('research-trace').replaceChildren();$('research-status').textContent='';for(const id of ['research-cancel','research-retry','research-followup','research-report'])$(id).hidden=true;}};
$('research-older').onclick=()=>{state.historyOffset=(state.historyOffset||0)+20;refreshResearch();};
$('research-newer').onclick=()=>{state.historyOffset=Math.max(0,(state.historyOffset||0)-20);refreshResearch();};
$('research-cancel').onclick=async()=>{try{renderResearch(await api('/api/research/'+state.researchId+'/cancel',{method:'POST'}));}catch(error){toast(error.message);}};
$('research-retry').onclick=async()=>{state.requestStarted=performance.now();state.timerHistorical=false;state.requestFinished=null;state.timerRun=null;try{const run=await api('/api/research/'+state.researchId+'/retry',{method:'POST'});state.historyOffset=0;state.researchId=run.id;state.researchSequence=(state.researchSequence||0)+1;renderResearch(run);}catch(error){toast(error.message);}};
$('evidence-close').onclick=()=>$('evidence-drawer').close();
setInterval(refreshResearch,1200);
$('logout').onclick=async()=>{try{await api('/api/auth/logout',{method:'POST'});location.replace('/login');}catch(error){toast(error.message);}};
updateProjectControls();
(async()=>{try{state.session=await api('/api/session');$('account-name').textContent=state.session.user.name;$('upload-limit').textContent='Per file: '+bytes(state.session.max_file_bytes)+' · '+state.session.workers+' document workers';state.projects=(await api('/api/projects')).projects;const saved=localStorage.getItem('aegis.project:'+state.session.user.id);let identity=state.projects.some(p=>p.id===saved) ? saved : state.projects[0]?.id;const params=new URLSearchParams(location.search);let linked=null;if(params.has('document')){try{linked=await api('/api/documents/'+params.get('document'));identity=linked.project_id;}catch(error){toast(error.message);}}await chooseProject(identity);if(linked){await selectDocument(linked.id);if(state.summary&&linked.format==='pdf')await changePage(params.get('page')||1);if(state.summary&&params.has('chunk')){state.method='chunks';state.focusChunk=Number(params.get('chunk'));await loadView();const index=state.chunks.findIndex(chunk=>chunk.index===Number(params.get('chunk')));if(index>=0){state.chunkPosition=index;await loadChunk();}}}setInterval(refresh,1500);}catch(error){toast(error.message);}})();

function showView(view){
  if(!['chat','documents','search'].includes(view))view='chat';
  document.body.dataset.view=view;
  $('research-panel').hidden=view!=='chat';
  $('evidence-search').hidden=view!=='search';
  $('document-library').hidden=view!=='documents';$('chat-history').hidden=view!=='chat';
  for(const id of ['add','settings-button'])$(id).hidden=view!=='documents';
  document.querySelectorAll('button[data-view]').forEach(button=>{button.classList.toggle('active',button.dataset.view===view);});
  history.replaceState(null,'',location.pathname+location.search+'#'+view);
}
document.querySelectorAll('button[data-view]').forEach(button=>button.onclick=()=>showView(button.dataset.view));
$('new-chat').onclick=()=>{state.researchId=null;state.researchRun=null;state.requestStarted=null;state.requestFinished=null;$('research-timing').textContent='';$('research-panel').classList.remove('has-conversation');$('research-answer').replaceChildren();$('research-question').value='';$('research-status').textContent='';for(const id of ['research-cancel','research-retry','research-followup','research-report'])$(id).hidden=true;showView('chat');$('research-question').focus();};
$('research-question').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();$('research-form').requestSubmit();}});
showView(new URLSearchParams(location.search).has('document')?'documents':location.hash.slice(1)||'chat');

function updateResearchTimer(){if(!state.requestStarted)return;const seconds=((state.requestFinished??performance.now())-state.requestStarted)/1000;$('research-timing').textContent=seconds.toFixed(1)+' s'+(state.requestFinished!==null?(state.timerHistorical?' · Recorded run time':' · Total response time'):' · Elapsed');}
setInterval(updateResearchTimer,100);
$('sidebar-toggle').onclick=()=>{document.body.classList.toggle('sidebar-collapsed');$('sidebar-toggle').setAttribute('aria-expanded',String(!document.body.classList.contains('sidebar-collapsed')));};

const mobileLayout=matchMedia('(max-width:760px)');function adaptSidebar(){document.body.classList.toggle('sidebar-collapsed',mobileLayout.matches);$('sidebar-toggle').setAttribute('aria-expanded',String(!mobileLayout.matches));}mobileLayout.addEventListener('change',adaptSidebar);adaptSidebar();

$('chat-history').before(document.querySelector('.saved-research'));
