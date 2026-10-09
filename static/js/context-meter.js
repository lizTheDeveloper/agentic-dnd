// The context window: the fill bar above the log, its per-component
// breakdown, and the card shown when compaction replaced old rounds.
'use strict';

// Context bar
function updateContextBar(state) {
  // Estimate context tokens from log
  const log = state.log || [];
  let totalChars = 0;
  log.forEach(entry => {
    totalChars += (entry.message || '').length;
    totalChars += (entry.reasoning || '').length;
    if (entry.tool_calls) {
      entry.tool_calls.forEach(tc => {
        totalChars += JSON.stringify(tc).length;
      });
    }
  });
  // Add system prompts
  const agents = state.agents || {};
  Object.values(agents).forEach(a => {
    totalChars += (a.system_prompt || '').length;
  });

  const estimatedTokens = Math.ceil(totalChars / 4);
  const modelWindow = 32000; // conservative estimate
  const pct = Math.min(100, Math.round((estimatedTokens / modelWindow) * 100));

  const fill = $('#context-fill');
  fill.style.width = pct + '%';
  fill.className = 'context-bar__fill' + (pct > 80 ? ' danger' : pct > 50 ? ' warn' : '');
  $('#context-label').textContent = 'Context: ~' + fmtNum(estimatedTokens) + ' / ' + fmtNum(modelWindow) + ' tokens';

  // Update breakdown if open
  if (contextBreakdownOpen) renderContextBreakdown(state);
}

function renderContextBreakdown(state) {
  var el = $('#context-breakdown');
  el.innerHTML = '';

  // Get breakdown from most recent log entry with context_estimate
  var log = state.log || [];
  var breakdown = null;
  var total = 0;
  for (var i = log.length - 1; i >= 0; i--) {
    if (log[i].context_estimate && log[i].context_estimate.breakdown) {
      breakdown = log[i].context_estimate.breakdown;
      total = log[i].context_estimate.total || 0;
      break;
    }
  }

  var title = document.createElement('div');
  title.className = 'context-breakdown__title';
  title.textContent = 'Context Breakdown' + (total ? ' (' + fmtNum(total) + ' tokens)' : '');
  el.appendChild(title);

  if (!breakdown) {
    var hint = document.createElement('div');
    hint.className = 'context-breakdown__hint';
    hint.textContent = 'No detailed breakdown available yet. Play a few turns to see context allocation.';
    el.appendChild(hint);
    return;
  }

  var modelWindow = 32000;
  var categories = [
    { key: 'system', label: 'System prompt', color: 'system' },
    { key: 'tools', label: 'Tool schemas', color: 'tools' },
    { key: 'history', label: 'History', color: 'history' },
    { key: 'facts', label: 'World facts', color: 'facts' },
    { key: 'notes', label: 'Agent notes', color: 'notes' },
  ];

  categories.forEach(function(cat) {
    var val = breakdown[cat.key] || 0;
    if (val === 0) return;
    var pct = Math.round((val / modelWindow) * 100);
    var row = document.createElement('div');
    row.className = 'token-bar-row';
    var label = document.createElement('span');
    label.className = 'token-bar-label';
    label.textContent = cat.label;
    var bar = document.createElement('div');
    bar.className = 'token-bar';
    var barFill = document.createElement('div');
    barFill.className = 'token-bar__fill ' + cat.color;
    barFill.style.width = Math.min(100, pct) + '%';
    bar.appendChild(barFill);
    var valSpan = document.createElement('span');
    valSpan.className = 'token-bar-val';
    valSpan.textContent = fmtNum(val);
    row.appendChild(label);
    row.appendChild(bar);
    row.appendChild(valSpan);
    el.appendChild(row);
  });

  var totalPct = Math.round((total / modelWindow) * 100);
  if (totalPct > 70) {
    var hint = document.createElement('div');
    hint.className = 'context-breakdown__hint';
    hint.textContent = 'Context is ' + totalPct + '% full. Compaction will trigger soon — facts will be extracted and history summarized.';
    el.appendChild(hint);
  }
}

// Build enhanced compaction card
function buildCompactionCard(entry, state) {
  var card = document.createElement('div');
  card.className = 'turn-card compaction-enhanced';

  // Banner
  var banner = document.createElement('div');
  banner.className = 'compaction-banner';
  banner.textContent = 'CONTEXT COMPACTED — Round ' + entry.round;
  card.appendChild(banner);

  // Stats
  var tokensBefore = entry.tokens_before || 0;
  var tokensAfter = entry.tokens_after || 0;
  var factsExtracted = entry.facts_extracted || [];
  var roundsCompacted = entry.rounds_compacted || [];

  if (tokensBefore || tokensAfter) {
    var stats = document.createElement('div');
    stats.className = 'compaction-stats';

    var beforeStat = document.createElement('span');
    beforeStat.className = 'compaction-stat';
    beforeStat.innerHTML = '<span class="compaction-stat__label">Before: </span><span class="compaction-stat__val before">' + fmtNum(tokensBefore) + ' tokens (' + Math.round((tokensBefore / 32000) * 100) + '%)</span>';
    stats.appendChild(beforeStat);

    var afterStat = document.createElement('span');
    afterStat.className = 'compaction-stat';
    afterStat.innerHTML = '<span class="compaction-stat__label">After: </span><span class="compaction-stat__val after">' + fmtNum(tokensAfter) + ' tokens (' + Math.round((tokensAfter / 32000) * 100) + '%)</span>';
    stats.appendChild(afterStat);

    card.appendChild(stats);
  }

  // Facts extracted
  if (Array.isArray(factsExtracted) && factsExtracted.length > 0) {
    var factsDiv = document.createElement('div');
    factsDiv.className = 'compaction-facts';
    var factsHeader = document.createElement('div');
    factsHeader.className = 'td-section__header';
    factsHeader.textContent = 'Facts extracted: ' + factsExtracted.length;
    factsDiv.appendChild(factsHeader);

    var showCount = Math.min(factsExtracted.length, 5);
    for (var i = 0; i < showCount; i++) {
      var f = factsExtracted[i];
      var fe = document.createElement('div');
      fe.className = 'compaction-fact';
      fe.textContent = '"' + (f.fact || JSON.stringify(f)) + '" (' + (f.category || '?') + ')';
      factsDiv.appendChild(fe);
    }
    if (factsExtracted.length > showCount) {
      var more = document.createElement('div');
      more.className = 'compaction-fact';
      more.style.color = 'var(--ink-dim)';
      more.textContent = '... and ' + (factsExtracted.length - showCount) + ' more';
      factsDiv.appendChild(more);
    }
    card.appendChild(factsDiv);
  }

  // Summary
  var summaryText = (entry.summary || entry.message || '').replace(/^Previously:\s*/i, '');
  if (summaryText) {
    var summary = document.createElement('div');
    summary.className = 'compaction-summary';
    summary.textContent = summaryText;
    card.appendChild(summary);
  }

  // Rounds info
  if (roundsCompacted.length > 0) {
    var roundsInfo = document.createElement('div');
    roundsInfo.className = 'compaction-rounds';
    roundsInfo.textContent = 'History compressed: rounds ' + roundsCompacted[0] + '-' + roundsCompacted[roundsCompacted.length - 1] + ' → summary';
    card.appendChild(roundsInfo);
  }

  return card;
}
