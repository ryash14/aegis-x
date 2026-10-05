/* Render saved artifacts only; document text is inserted using textContent. */
let mode = location.hash === '#ocr' ? 'ocr' : 'normalized';
let runData = null;
let runEntry = null;
let documentIndex = 0;
let pageIndex = 0;
let overlayVisible = true;
let loadSequence = 0;
const catalog = window.AEGIS_EXPERIMENTS || [];
const $ = id => document.getElementById(id);
const modes = {
  baseline: ['Baseline text', 'Sorted plain text, preserved unchanged.'],
  layout: ['Layout & reading order', 'Native blocks in geometric reading order.'],
  normalized: ['Normalized text', 'Conservative edits with original source ranges.'],
  ocr: ['Local OCR', 'Offline recognition · native text preserved separately.'],
};
function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}
function option(select, text, value) {
  const item = node('option', '', text);
  item.value = value;
  select.append(item);
}
function loadRun(entry) {
  const sequence = ++loadSequence;
  $('loading').hidden = false;
  $('loading').textContent = 'Loading saved run…';
  $('workspace').hidden = true;
  runEntry = entry;
  const script = document.createElement('script');
  script.src = '../data/experiments/runs/' + encodeURIComponent(entry.id) + '/run.js';
  script.onload = () => {
    script.remove();
    if (sequence !== loadSequence) return;
    runData = window.AEGIS_RUN;
    documentIndex = 0;
    pageIndex = 0;
    renderDocuments();
    $('loading').hidden = true;
    $('workspace').hidden = false;
  };
  script.onerror = () => {
    script.remove();
    if (sequence === loadSequence) $('loading').textContent = 'Saved run unavailable. Rebuild the snapshot.';
  };
  document.head.append(script);
}
function renderDocuments() {
  $('document').replaceChildren();
  runData.documents.forEach((doc, index) => option($('document'), doc.name, index));
  $('document').value = documentIndex;
  renderPages();
}
function renderPages() {
  const doc = runData.documents[documentIndex];
  $('page').replaceChildren();
  doc.pages.forEach((page, index) => option($('page'), 'Page ' + page.number, index));
  pageIndex = Math.min(pageIndex, Math.max(0, doc.pages.length - 1));
  $('page').value = pageIndex;
  $('page').disabled = doc.pages.length === 0;
  render();
}
function activate(identifier, sources = [identifier]) {
  const ids = new Set(sources.map(String));
  for (const box of $('overlay').children) box.classList.toggle('active', ids.has(box.dataset.block));
  for (const block of $('output').querySelectorAll('.block')) {
    block.classList.toggle('active', block.dataset.block === String(identifier));
  }
}
function detailsLine(label, text) {
  const line = node('p');
  line.append(node('strong', '', label + ' · '), document.createTextNode(text));
  $('provenance').append(line);
}
function renderBlock(block, normalized) {
  const text = normalized ? block.normalized_text : block.text;
  if (!text) return;
  const sources = normalized ? block.normalization_sources : [block.block_id];
  const row = node('div', 'block');
  row.tabIndex = 0;
  row.dataset.block = block.block_id;
  const title = node('div', 'block-title');
  title.append(node('span', '', 'Block ' + block.number), node('span', '', mode === 'ocr' ? 'Mean confidence ' + block.confidence + ' · ' + block.low_confidence_words + ' low' : normalized ? block.normalization_changes + ' edits' : 'ID ' + block.block_id));
  row.append(title, node('p', '', text));
  for (const event of ['mouseenter', 'focus', 'click']) row.addEventListener(event, () => activate(block.block_id, sources));
  $('output').append(row);
}
function renderOverlay(block) {
  const box = node('div', 'box');
  box.dataset.block = block.block_id;
  const [left, top, width, height] = block.display_box;
  Object.assign(box.style, {left: left + '%', top: top + '%', width: width + '%', height: height + '%'});
  box.append(node('span', '', String(block.number)));
  box.addEventListener('click', () => {
    const row = [...$('output').querySelectorAll('.block')].find(item => item.dataset.block === String(block.block_id));
    if (row) {
      row.click();
      row.scrollIntoView({block: 'nearest'});
    }
  });
  $('overlay').append(box);
}
function render() {
  const doc = runData.documents[documentIndex];
  const page = doc.pages[pageIndex];
  const normalized = mode === 'normalized';
  const ocrMode = mode === 'ocr';
  const unavailable = page && (normalized && !page.normalized || ocrMode && !page.ocr_included);
  $('heading').textContent = modes[mode][0];
  $('description').textContent = modes[mode][1];
  document.querySelectorAll('[data-mode]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.mode === mode)));
  $('previous').disabled = pageIndex === 0 || !page;
  $('next').disabled = !page || pageIndex === doc.pages.length - 1;
  $('overlay-toggle').disabled = mode === 'baseline' || !page || unavailable;
  $('overlay').hidden = mode === 'baseline' || !overlayVisible || unavailable;
  $('overlay-toggle').setAttribute('aria-pressed', String(overlayVisible));
  $('facts').replaceChildren();
  const facts = [['Pages', doc.page_count || '—'], ['Status', doc.status], ['Parser', runData.parser_version]];
  if (normalized && page?.normalized) facts.push(['Edits', page.normalized.blocks.reduce((count, block) => count + block.changes.length, 0)]);
  for (const [label, value] of facts) {
    const fact = node('span');
    fact.append(document.createTextNode(label + ' '), node('strong', '', String(value)));
    $('facts').append(fact);
  }
  $('warning').textContent = ocrMode ? (!page ? doc.error || 'No preview.' : unavailable ? 'OCR was not included in this saved run.' : page.ocr_error ? page.ocr_error.message : page.ocr ? page.ocr.warnings.join(' · ') : 'Native text present; OCR skipped. Mixed image regions may still need OCR.') : doc.error || (unavailable ? 'Normalization was not included in this saved run.' : normalized ? 'Hard hyphens and internal spacing preserved. Headers remain.' : mode === 'layout' ? 'Heuristic order · Tables, equations and drop caps need review.' : 'Columns may interleave.');
  $('overlay').replaceChildren();
  $('output').replaceChildren();
  $('provenance').replaceChildren();
  detailsLine('Source', doc.source);
  detailsLine('SHA-256', doc.document_id || 'Unavailable');
  detailsLine('Run', runData.id);
  detailsLine('Method', ocrMode ? page?.ocr?.method || 'Not run' : normalized ? runData.normalization_algorithm || 'Unavailable in this run' : mode === 'layout' ? runData.layout_algorithm : 'PyMuPDF text / sort=True');
  detailsLine('Scope', 'Sample pages only. Earlier runs retain their original outputs.');
  if (ocrMode && page?.ocr) {
    const ocr = page.ocr;
    detailsLine('Engine', ocr.engine.version);
    detailsLine('Settings', `${ocr.config.language} · ${ocr.config.dpi} DPI · ${ocr.elapsed_seconds} s`);
    detailsLine('Orientation', `${ocr.rotation_correction}° correction · confidence ${ocr.orientation_confidence ?? 'unavailable'}`);
    detailsLine('Coordinates', 'Unrotated PDF points; word offsets reference OCR text, separate from native text.');
    for (const [language, hash] of ocr.engine.model_sha256) detailsLine('Model ' + language, hash);
    for (const word of ocr.words.filter(word => word.confidence < ocr.config.low_confidence_threshold)) {
      detailsLine('Review', `${word.text} · confidence ${word.confidence.toFixed(1)} · [${word.output_start}, ${word.output_end})`);
    }
  }
  if (doc.parser_diagnostics) detailsLine('Parser diagnostics', doc.parser_diagnostics);
  $('page-image').hidden = !page;
  $('source-empty').hidden = !!page;
  if (!page) {
    $('source-empty').textContent = doc.error || 'No preview available.';
    $('output').append(node('div', 'empty', doc.error || 'No extraction available.'));
    $('source-label').textContent = 'Unavailable';
    $('output-label').textContent = 'No text';
    return;
  }
  $('source-label').textContent = 'Physical page ' + page.number;
  $('image').src = '../data/experiments/runs/' + encodeURIComponent(runEntry.id) + '/' + encodeURIComponent(page.image);
  $('image').alt = doc.name + ', physical page ' + page.number;
  $('source-scroll').scrollTop = 0;
  $('output').scrollTop = 0;
  $('output-header').firstChild.textContent = mode === 'baseline' ? 'Sorted text ' : ocrMode ? 'Recognized lines ' : normalized ? 'Normalized blocks ' : 'Ordered blocks ';
  if (mode === 'baseline') {
    $('output-label').textContent = page.baseline_text.length.toLocaleString() + ' characters';
    $('output').append(node('pre', 'baseline', page.baseline_text || 'No extractable text.'));
  } else if (ocrMode) {
    const blocks = page.ocr_blocks || [];
    $('output-label').textContent = page.ocr ? page.ocr.words.length + ' words' : 'Not run';
    if (!blocks.length) $('output').append(node('div', 'empty', unavailable ? 'Choose an OCR run.' : page.ocr_error?.message || (page.ocr ? 'No text recognized.' : 'Native text present; inspect Baseline or Normalized.')));
    blocks.forEach(block => { renderBlock(block, false); renderOverlay(block); });
  } else if (unavailable) {
    $('output-label').textContent = 'Unavailable';
    $('output').append(node('div', 'empty', 'Choose a newer run to inspect normalized text.'));
  } else {
    const blocks = page.blocks.filter(block => !normalized || block.normalized_text);
    $('output-label').textContent = blocks.length + ' blocks';
    if (!blocks.length) $('output').append(node('div', 'empty', 'No text blocks. OCR is required.'));
    blocks.forEach(block => renderBlock(block, normalized));
    page.blocks.forEach(renderOverlay);
    const warnings = normalized ? page.normalized.warnings : page.warnings;
    warnings.forEach(warning => detailsLine('Note', warning));
    if (normalized) {
      detailsLine('Mapping', 'Offsets refer to Unicode characters in native text lines. Inserted separators are marked explicitly.');
      for (const block of page.normalized.blocks) {
        for (const change of block.changes) {
          const origins = change.sources.map(source => `block ${source.block_id}, line ${source.line_index + 1}, [${source.start}, ${source.end})`).join('; ');
          detailsLine(change.rule, `${JSON.stringify(change.before)} → ${JSON.stringify(change.after)} · ${origins}`);
        }
      }
    }
  }
}
$('run').addEventListener('change', event => loadRun(catalog.find(entry => entry.id === event.target.value)));
$('document').addEventListener('change', event => {documentIndex = Number(event.target.value); pageIndex = 0; renderPages();});
$('page').addEventListener('change', event => {pageIndex = Number(event.target.value); render();});
$('previous').addEventListener('click', () => {if (pageIndex > 0) {pageIndex--; $('page').value = pageIndex; render();}});
$('next').addEventListener('click', () => {if (pageIndex < runData.documents[documentIndex].pages.length - 1) {pageIndex++; $('page').value = pageIndex; render();}});
$('overlay-toggle').addEventListener('click', () => {overlayVisible = !overlayVisible; render();});
document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => {mode = button.dataset.mode; if (runData) render();}));
if (catalog.length) {
  const format = new Intl.DateTimeFormat('en', {timeZone: 'Asia/Kolkata', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  [...catalog].reverse().forEach(entry => option($('run'), format.format(new Date(entry.created_at)) + ' IST · ' + entry.documents + ' docs', entry.id));
  $('run').value = catalog[catalog.length - 1].id;
  loadRun(catalog[catalog.length - 1]);
} else {
  $('loading').textContent = 'No saved experiments yet. Run: uv run python scripts/build_ingestion_demo.py';
}
