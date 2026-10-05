/* Local snapshots; source text is always inserted as text, never HTML. */
const $ = id => document.getElementById(id);
const catalog = window.AEGIS_CHUNK_CATALOG || [];
let runData = null, chunkData = null, runEntry = null, documentIndex = 0, chunkIndex = 0;
let runSequence = 0, documentSequence = 0;
function node(tag, text, className) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; }
function option(select, text, value) { const item = node('option', text); item.value = value; select.append(item); }
function loadScript(path, callback) { const script = node('script'); script.src = path; script.onload = () => { script.remove(); callback(); }; script.onerror = () => { script.remove(); $('message').textContent = 'Saved artifact unavailable.'; }; document.head.append(script); }
function marked(text, ranges) {
  const fragment = document.createDocumentFragment(), chars = Array.from(text);
  let cursor = 0;
  for (const [start, end] of ranges.sort((a, b) => a[0] - b[0])) {
    if (end <= cursor) continue;
    if (start > cursor) fragment.append(document.createTextNode(chars.slice(cursor, start).join('')));
    fragment.append(node('mark', chars.slice(Math.max(cursor, start), end).join(''))); cursor = end;
  }
  fragment.append(document.createTextNode(chars.slice(cursor).join(''))); return fragment;
}
function reset() { for (const id of ['sources', 'text', 'details', 'facts', 'section']) $(id).replaceChildren(); $('count').textContent = ''; $('previous').disabled = true; $('next').disabled = true; }
function loadRun(entry) {
  runEntry = entry; const sequence = ++runSequence; ++documentSequence;
  chunkData = null; reset(); $('message').textContent = 'Loading run…';
  loadScript('../data/chunk-experiments/runs/' + entry.id + '/run.js', () => {
    if (sequence !== runSequence) return;
    runData = window.AEGIS_CHUNK_RUN; $('document').replaceChildren();
    runData.documents.forEach((item, index) => option($('document'), item.name, index));
    const nasa = runData.documents.findIndex(item => item.name.includes('nasa-systems'));
    documentIndex = Math.max(0, nasa); $('document').value = documentIndex; loadDocument();
  });
}
function loadDocument() {
  const entry = runData.documents[documentIndex], sequence = ++documentSequence;
  chunkData = null; reset(); $('chunk').replaceChildren(); $('chunk').disabled = true;
  if (entry.status !== 'verified') { $('message').textContent = entry.error || 'Extraction failed.'; return; }
  $('message').textContent = 'Loading document…';
  loadScript('../data/chunk-experiments/runs/' + runEntry.id + '/' + entry.file + '.js', () => {
    if (sequence !== documentSequence) return;
    chunkData = window.AEGIS_CHUNK_DOCUMENT;
    chunkData.unitById = new Map(chunkData.units.map(unit => [unit.unit_id, unit]));
    chunkData.chunks.forEach((chunk, index) => option($('chunk'), `${index + 1} · ${chunk.kind}`, index));
    const preface = entry.name.includes('nasa-systems') ? chunkData.chunks.findIndex(chunk => chunk.text.includes('This handbook') && chunk.mappings.some(mapping => mapping.sources.some(source => source.page === 11))) : -1;
    chunkIndex = Math.max(0, preface); $('chunk').value = chunkIndex; $('chunk').disabled = !chunkData.chunks.length; render();
  });
}
function sourceKey(source) { return source.xml_path || (source.domain === 'native_line' ? `native_line:${source.page}:${source.block_id}:${source.line_index}` : `${source.domain}:${source.page}`); }
function render() {
  reset(); const entry = runData.documents[documentIndex], chunk = chunkData?.chunks[chunkIndex];
  $('facts').append(node('span', `${entry.chunks} chunks`), node('span', `Limit ${runData.config.max_chars} characters`), node('span', 'Source coverage verified'));
  if (!chunk) { $('message').textContent = 'No text chunks in this document.'; return; }
  $('message').textContent = ''; $('section').textContent = chunk.headings.map(heading => heading.text).join(' / ');
  $('previous').disabled = chunkIndex === 0; $('next').disabled = chunkIndex === chunkData.chunks.length - 1;
  const overlap = chunk.fragments.reduce((sum, part) => sum + part.overlap_prefix_chars, 0);
  $('count').textContent = `${Array.from(chunk.text).length} characters · ${overlap} overlap`;
  $('text').append(marked(chunk.text, chunk.fragments.filter(part => part.overlap_prefix_chars).map(part => [part.output_start, part.output_start + part.overlap_prefix_chars])));
  for (const part of chunk.fragments) {
    const unit = chunkData.unitById.get(part.unit_id), chars = Array.from(unit.text);
    const start = Math.max(0, part.unit_start - 80), end = Math.min(chars.length, part.unit_end + 80);
    const row = node('div', undefined, 'unit');
    row.append(node('small', `${unit.kind} · [${part.unit_start}, ${part.unit_end}) · ${unit.unit_id}`));
    const pre = node('pre'); if (start) pre.append(document.createTextNode('…'));
    pre.append(marked(chars.slice(start, end).join(''), [[part.unit_start - start, part.unit_end - start]]));
    if (end < chars.length) pre.append(document.createTextNode('…')); row.append(pre); $('sources').append(row);
  }
  const link = node('a', 'Open original document'); link.href = 'file://' + encodeURI(chunk.source_path).replace(/#/g, '%23').replace(/\?/g, '%3F'); $('details').append(link);
  $('details').append(node('p', `Chunk ${chunk.chunk_id}`), node('p', `Document ${chunk.document_id}`), node('p', `Method ${chunk.algorithm} · Unicode character offsets`));
  chunk.warnings.forEach(warning => $('details').append(node('p', warning)));
  const origins = new Map();
  for (const mapping of chunk.mappings) for (const source of mapping.sources) {
    const key = sourceKey(source); if (!origins.has(key)) origins.set(key, {source, ranges: [], confidences: []});
    origins.get(key).ranges.push([source.start, source.end]);
    if (source.confidence !== null) origins.get(key).confidences.push(source.confidence);
  }
  for (const [key, item] of origins) {
    const row = node('div', undefined, 'refs'); row.append(node('code', key));
    const pre = node('pre'); pre.append(marked(chunkData.source_text[key], item.ranges)); row.append(pre);
    if (item.source.bbox) row.append(node('small', `PDF box ${item.source.bbox.map(value => value.toFixed(1)).join(', ')} points`));
    if (item.confidences.length) row.append(node('small', ` · OCR confidence mean ${(item.confidences.reduce((a, b) => a + b, 0) / item.confidences.length).toFixed(1)} across ${item.confidences.length} word mappings`));
    $('details').append(row);
  }
}
$('run').onchange = event => loadRun(catalog.find(entry => entry.id === event.target.value));
$('document').onchange = event => { documentIndex = Number(event.target.value); loadDocument(); };
$('chunk').onchange = event => { chunkIndex = Number(event.target.value); render(); };
$('previous').onclick = () => { if (chunkIndex > 0) { chunkIndex--; $('chunk').value = chunkIndex; render(); } };
$('next').onclick = () => { if (chunkIndex < chunkData.chunks.length - 1) { chunkIndex++; $('chunk').value = chunkIndex; render(); } };
if (catalog.length) {
  const format = new Intl.DateTimeFormat('en', {timeZone: 'Asia/Kolkata', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  [...catalog].reverse().forEach(entry => option($('run'), `${entry.documents} docs · ${format.format(new Date(entry.created_at))} IST`, entry.id));
  $('run').value = catalog.at(-1).id; loadRun(catalog.at(-1));
}
