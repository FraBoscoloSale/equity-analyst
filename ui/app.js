'use strict';
const $ = id => document.getElementById(id);
let token = '', polling = false, reportText = '', lastJob = null;

// Render the saved Markdown without executing report HTML or remote scripts.
// All report text becomes text nodes; links allow only HTTP(S) destinations.
function inline(parent, text) {
  const pieces = /(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\)|\*\*[^*\n]+\*\*|`[^`\n]+`)/g;
  let end = 0;
  for (const match of text.matchAll(pieces)) {
    parent.append(document.createTextNode(text.slice(end, match.index)));
    const piece = match[0];
    let element;
    if (piece.startsWith('[')) {
      const link = piece.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
      element = document.createElement('a'); element.textContent = link[1];
      element.href = link[2]; element.target = '_blank'; element.rel = 'noopener noreferrer';
    } else {
      element = document.createElement(piece.startsWith('**') ? 'strong' : 'code');
      element.textContent = piece.slice(piece.startsWith('**') ? 2 : 1, piece.startsWith('**') ? -2 : -1);
    }
    parent.append(element); end = match.index + piece.length;
  }
  parent.append(document.createTextNode(text.slice(end)));
}
function cells(line) { return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(v => v.trim().replace(/\\\|/g, '|')); }
function renderMarkdown(target, markdown) {
  target.replaceChildren();
  const lines = markdown.replace(/<!--[\s\S]*?-->/g, '').split(/\r?\n/);
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.startsWith('|') && i+1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i+1])) {
      const wrap = document.createElement('div'); wrap.className = 'table-scroll';
      const table = document.createElement('table'), head = document.createElement('thead'), body = document.createElement('tbody');
      const row = document.createElement('tr');
      for (const value of cells(line)) { const th = document.createElement('th'); th.scope = 'col'; inline(th, value); row.append(th); }
      head.append(row); i += 2;
      while (i < lines.length && lines[i].startsWith('|')) {
        const tr = document.createElement('tr');
        for (const value of cells(lines[i++])) { const td = document.createElement('td'); inline(td, value); tr.append(td); }
        body.append(tr);
      }
      table.append(head, body); wrap.append(table); target.append(wrap); continue;
    }
    if (line.startsWith('```')) {
      const pre = document.createElement('pre'); const text = []; i++;
      while (i < lines.length && !lines[i].startsWith('```')) text.push(lines[i++]);
      pre.textContent = text.join('\n'); target.append(pre); i++; continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)/);
    const element = document.createElement(heading ? 'h'+heading[1].length : line.startsWith('>') ? 'blockquote' : 'p');
    inline(element, heading ? heading[2] : line.replace(/^>\s?/, ''));
    target.append(element); i++;
  }
}
async function api(path, options = {}) {
  const response = await fetch(path, {cache:'no-store', ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The page could not contact the local server.');
  return data;
}
function error(message) { $('error').textContent = message; $('error').hidden = false; }
function show(job) {
  lastJob = job; const active = job.status === 'running';
  $('run-button').disabled = active;
  $('run-button').firstChild.textContent = active ? 'Team running… ' : 'Run team ';
  $('status').textContent = active ? 'Running' : job.status === 'failed' ? 'Failed' : 'Verification complete';
  $('status').className = 'status ' + job.status;
  $('progress-message').textContent = active ? 'The team is working. This can take several minutes.' : job.status === 'failed' ? 'The run did not complete. No final verified report is available.' : 'All nodes have finished, including the mandatory Verifier. Review the marks and findings below.';
  if ($('nodes').childElementCount > job.nodes.length) $('nodes').replaceChildren();
  for (const name of job.nodes.slice($('nodes').childElementCount)) {
    const li = document.createElement('li'); li.textContent = name; $('nodes').append(li);
  }
  if (job.status === 'failed') { $('results').hidden = true; error(job.error); }
  if (job.status === 'completed') {
    $('error').hidden = true; reportText = job.report;
    $('results').hidden = false; $('counts').replaceChildren();
    for (const mark of ['correct','wrong','unsupported']) {
      const badge = document.createElement('span'); badge.className = mark;
      badge.textContent = `${job.counts[mark] || 0} ${mark}`; $('counts').append(badge);
    }
    $('findings').hidden = !(job.unresolved_parts || job.counts.wrong || job.counts.unsupported);
    $('findings').textContent = `Verification completed with findings. ${job.unresolved_parts} report part(s) retain unresolved Manager findings. Check the verification table for corrections and unsupported numbers.`;
    renderMarkdown($('report'), job.report);
  }
}
async function poll(id) {
  if (polling) return;
  polling = true;
  try {
    while (true) {
      const job = await api('/api/runs/'+id); show(job);
      if (job.status !== 'running') break;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  } catch (exc) {
    error('Progress connection interrupted. The run may still be working. Reload this page to reconnect.');
    $('status').textContent = 'Connection interrupted';
    // Keep Run disabled: lost connectivity does not mean the worker stopped.
  } finally { polling = false; }
}
$('run-form').addEventListener('submit', async event => {
  event.preventDefault(); $('error').hidden = true; $('run-button').disabled = true;
  try {
    const response = await api('/api/runs', {method:'POST', headers:{'Content-Type':'application/json','X-Run-Token':token},
      body:JSON.stringify({ticker:$('ticker').value,peers:$('peers').value,price:$('price').value})});
    reportText = ''; $('report').replaceChildren(); $('nodes').replaceChildren(); $('results').hidden = true;
    poll(response.id);
  } catch (exc) { error(exc.message); $('run-button').disabled = false; }
});
$('download').addEventListener('click', () => {
  if (!reportText) return;
  const url = URL.createObjectURL(new Blob([reportText], {type:'text/markdown;charset=utf-8'}));
  const a = document.createElement('a'); a.href = url; a.download = 'verified_report.md'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
(async () => {
  try {
    const session = await api('/api/session'); token = session.token;
    if (session.run) { show(session.run); if (session.run.status === 'running') poll(session.run.id); }
    else $('run-button').disabled = false;
  } catch (exc) { error('Cannot connect to the local server. Reload the page after starting it.'); }
})();
