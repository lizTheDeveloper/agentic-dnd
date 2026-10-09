// Rulebook panel on the setup screen: pick a strategy, chunk the pasted
// text, preview the first few chunks with size stats.
'use strict';

const STRATEGY_INFO = {
  header:    { name: 'Header-based', desc: 'Split on markdown headings (##, ###). Best for structured docs.', icon: '#' },
  paragraph: { name: 'Paragraph',    desc: 'Split on double newlines. Best for prose without headings.',     icon: '¶' },
  fixed:     { name: 'Fixed length', desc: 'Split every N characters. Consistent size, may split mid-rule.', icon: '✂' },
  semantic:  { name: 'Semantic',     desc: 'Header split + sub-split long sections by paragraph.',           icon: '⦿' },
};

function initChunkerUI() {
  var container = $('#chunker-strategies');
  ['header','paragraph','fixed','semantic'].forEach(function(key) {
    var info = STRATEGY_INFO[key];
    var card = document.createElement('label');
    card.className = 'chunker-strategy-card' + (key === chunkStrategy ? ' active' : '');
    card.dataset.strategy = key;
    card.innerHTML =
      '<input type="radio" name="chunk-strategy" value="' + key + '"' + (key === chunkStrategy ? ' checked' : '') + '>' +
      '<span class="chunker-strategy-icon">' + info.icon + '</span>' +
      '<span class="chunker-strategy-name">' + info.name + '</span>' +
      '<span class="chunker-strategy-desc">' + info.desc + '</span>';
    card.querySelector('input').addEventListener('change', function() {
      chunkStrategy = key;
      container.querySelectorAll('.chunker-strategy-card').forEach(function(c) {
        c.classList.toggle('active', c.dataset.strategy === key);
      });
      $('#chunker-fixed-slider').classList.toggle('visible', key === 'fixed');
      runChunker();
    });
    container.appendChild(card);
  });
}
initChunkerUI();

// Fixed size slider
$('#rng-chunk-size').addEventListener('input', function() {
  chunkFixedSize = parseInt(this.value, 10);
  $('#chunk-size-val').textContent = chunkFixedSize;
  runChunker();
});

function runChunker() {
  var text = $('#inp-rulebook-text').value.trim();
  if (!text) {
    rulebookChunks = [];
    $('#chunker-results').classList.remove('visible');
    return;
  }
  rulebookChunks = doChunk(text, chunkStrategy, chunkFixedSize);
  var st = chunkStats(rulebookChunks);
  var resultsEl = $('#chunker-results');
  resultsEl.classList.add('visible');
  $('#chunker-results-header').textContent =
    'Your rulebook was split into ' + st.count + ' chunks using ' + STRATEGY_INFO[chunkStrategy].name;

  var statsRow = $('#chunker-stats-row');
  statsRow.innerHTML = '';
  if (st.count > 0) {
    statsRow.innerHTML =
      '<span class="chunker-stat"><strong>' + st.count + '</strong> chunks</span>' +
      '<span class="chunker-stat">min <strong>' + st.min + '</strong> tokens</span>' +
      '<span class="chunker-stat">max <strong>' + st.max + '</strong> tokens</span>' +
      '<span class="chunker-stat">avg <strong>' + st.avg + '</strong></span>' +
      '<span class="chunker-stat">median <strong>' + st.median + '</strong></span>';
  }

  // Preview first 3 chunks
  var preview = $('#chunker-preview');
  preview.innerHTML = '';
  var show = Math.min(rulebookChunks.length, 3);
  for (var i = 0; i < show; i++) {
    var c = rulebookChunks[i];
    var card = document.createElement('div');
    card.className = 'chunker-preview-card';
    var pText = c.content.length > 120 ? c.content.substring(0,117) + '...' : c.content;
    var headerDiv = document.createElement('div');
    headerDiv.className = 'chunker-preview-header';
    headerDiv.innerHTML =
      '<span class="chunker-preview-id">' + esc(c.id) + '</span>' +
      '<span class="chunker-preview-title">' + esc(c.title) + '</span>' +
      '<span class="chunker-preview-tokens">' + c.tokens_est + ' tokens</span>';
    var contentDiv = document.createElement('div');
    contentDiv.className = 'chunker-preview-content';
    contentDiv.textContent = pText;
    card.appendChild(headerDiv);
    card.appendChild(contentDiv);
    preview.appendChild(card);
  }
  if (rulebookChunks.length > show) {
    var more = document.createElement('div');
    more.className = 'chunker-preview-more';
    more.textContent = '+ ' + (rulebookChunks.length - show) + ' more chunks';
    preview.appendChild(more);
  }
}

$('#btn-chunk-rulebook').addEventListener('click', runChunker);
$('#btn-rechunk').addEventListener('click', function() {
  $('#chunker-results').classList.remove('visible');
});

// Try Cairn 2e example
$('#btn-try-cairn').addEventListener('click', async function() {
  this.disabled = true;
  this.textContent = 'Loading...';
  try {
    var res = await fetch('/static/examples/cairn-srd-example.txt');
    if (res.ok) {
      var text = await res.text();
      $('#inp-rulebook-text').value = text;
      $('#inp-rulebook-name').value = 'Cairn 2e';
      runChunker();
      toast('Cairn 2e SRD loaded (CC-BY-SA 4.0, Yochai Gal)');
    } else {
      toast('Could not load Cairn SRD');
    }
  } catch(e) {
    toast('Network error loading example');
  }
  this.disabled = false;
  this.textContent = 'Try Cairn 2e (CC-BY-SA)';
});
