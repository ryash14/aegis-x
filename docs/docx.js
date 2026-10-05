const runs = document.getElementById('runs');
const documents = document.getElementById('documents');
const content = document.getElementById('content');
function node(tag, text) { const element = document.createElement(tag); if (text !== undefined) element.textContent = text; return element; }
function renderBlock(block) {
  if (block.kind === 'table') {
    const table = node('table');
    for (const row of block.children.filter(x => x.kind === 'row')) {
      const tr = node('tr');
      for (const cell of row.children.filter(x => x.kind === 'cell')) {
        const td = node('td'); td.colSpan = Math.max(1, Math.min(100, cell.column_span));
        if (cell.vertical_merge) td.append(node('small', `Vertical merge: ${cell.vertical_merge}`));
        for (const child of cell.children) td.append(renderBlock(child));
        tr.append(td);
      }
      table.append(tr);
    }
    return table;
  }
  const article = node('article');
  article.append(node(block.heading_level ? 'h3' : 'p', block.text || '(empty paragraph)'));
  if (block.image_ids.length) article.append(node('small', `Images: ${block.image_ids.join(', ')}`));
  const details = node('details'); details.append(node('summary', 'Source position'), node('code', block.source)); article.append(details);
  return article;
}
function show() {
  const item = window.docxRun.documents[Number(documents.value)];
  if (!item) return;
  const doc = item.document; content.replaceChildren();
  const link = node('a', 'Open original DOCX'); link.href = `file://${doc.source_path}`; content.append(link);
  const details = node('details'); details.append(node('summary', 'Provenance and limitations'), node('p', `${doc.document_id}\n${doc.warnings.join('\n')}`));
  for (const image of doc.images) details.append(node('p', `${image.relationship_id}: ${image.target}\n${image.sha256 || 'External / unavailable'}`));
  content.append(details);
  for (const block of doc.blocks) content.append(renderBlock(block));
}
function load() {
  const script = node('script'); script.src = `../data/docx-experiments/${runs.value}.js`;
  script.onload = () => { documents.replaceChildren(); window.docxRun.documents.forEach((item, i) => { const option = node('option', item.name); option.value = i; documents.append(option); }); show(); script.remove(); };
  script.onerror = () => { content.textContent = 'Snapshot unavailable.'; script.remove(); };
  document.head.append(script);
}
for (const id of window.docxCatalog || []) { const option = node('option', id); option.value = id; runs.append(option); }
runs.onchange = load; documents.onchange = show;
if (runs.options.length) { runs.selectedIndex = runs.options.length - 1; load(); }
