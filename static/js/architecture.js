// Architecture panel: campaign stats and the 12-factor agent scorecard.
'use strict';

// ----------------------------------------------------------------
// 12-factor definitions
// ----------------------------------------------------------------
const TWELVE_FACTORS = [
  { id: 'instructions', name: 'Natural-language instructions', check: function(s) { return Object.values(s.agents||{}).some(function(a){ return (a.system_prompt||'').length > 50; }); } },
  { id: 'tools', name: 'Tool use', check: function(s) { return Object.values(s.agents||{}).some(function(a){ return (a.tools||[]).length >= 2; }); } },
  { id: 'retrieval', name: 'Retrieval (RAG)', check: function(s) { return (s.rulebook_chunks||0) > 0; } },
  { id: 'context', name: 'Context management', check: function(s) { return (s.log||[]).some(function(e){ return e.type === 'compaction' || (e.context_estimate && e.context_estimate.total > 0); }); } },
  { id: 'structured', name: 'Structured output', check: function(s) { return (s.log||[]).some(function(e){ return (e.tool_calls||[]).length > 0; }); } },
  { id: 'human', name: 'Human-in-the-loop', check: function(s) { return (s.log||[]).some(function(e){ return (e.whispers_received||[]).length > 0; }) || s.whispers_pending > 0; } },
  { id: 'planning', name: 'Planning', check: function(s) { return (s.log||[]).some(function(e){ return (e.reasoning||'').length > 30; }); } },
  { id: 'multiagent', name: 'Multi-agent', check: function(s) { return (s.agent_order||[]).length >= 2; } },
  { id: 'guardrails', name: 'Guardrails', check: function() { return true; } }, // rate limits + budget cap exist
  { id: 'state', name: 'State management', check: function(s) { return Object.keys(s.facts||{}).length > 0 || Object.keys(s.npcs||{}).length > 0; } },
  { id: 'observability', name: 'Observability', check: function(s) { return (s.log||[]).some(function(e){ return e.tokens_in > 0 || (e.reasoning||'').length > 0; }); } },
  { id: 'evaluation', name: 'Evaluation', check: function() { return false; } }, // no eval suite in the tool
];

function score12Factors(state) {
  var passed = 0;
  var results = TWELVE_FACTORS.map(function(f) {
    var p = f.check(state);
    if (p) passed++;
    return { id: f.id, name: f.name, pass: p };
  });
  return { score: passed, total: 12, results: results };
}

// ---- Architecture Panel ----
function renderArchitecture(state) {
  var body = $('#arch-body');
  if (archCollapsed) return; // lazy — don't render if collapsed
  body.innerHTML = '';

  var agents = state.agents || {};
  var order = state.agent_order || [];
  var facts = state.facts || {};
  var dmAgent = null;
  var playerCount = 0;
  var dmToolCount = 0;
  var playerToolCount = 0;
  order.forEach(function(aid) {
    var a = agents[aid];
    if (!a) return;
    if (a.role === 'dm') {
      dmAgent = a;
      dmToolCount = (a.tools||[]).length;
    } else {
      playerCount++;
      playerToolCount = (a.tools||[]).length;
    }
  });

  // Stats section
  var stats = [
    ['Pattern', 'Multi-agent (turn-based)'],
    ['Agents', order.length + ' (' + (dmAgent ? '1 DM' : '0 DM') + ', ' + playerCount + ' player' + (playerCount !== 1 ? 's' : '') + ')'],
    ['DM tools', String(dmToolCount)],
    ['Player tools', String(playerToolCount)],
  ];
  if (state.rulebook_name) {
    stats.push(['Rulebook', state.rulebook_name + ' (' + (state.rulebook_chunks || 0) + ' chunks)']);
  }
  stats.push(['Memory', Object.keys(facts).length + ' facts']);

  // Estimate context from last log entry
  var log = state.log || [];
  var lastEntry = log.length > 0 ? log[log.length - 1] : null;
  var ctxEst = lastEntry && lastEntry.context_estimate ? lastEntry.context_estimate : null;
  if (ctxEst) {
    var total = ctxEst.total || 0;
    stats.push(['Context', '~' + fmtNum(total) + ' / 32k tokens']);
  }

  stats.forEach(function(row) {
    var el = document.createElement('div');
    el.className = 'arch-stat-row';
    var label = document.createElement('span');
    label.textContent = row[0];
    var val = document.createElement('span');
    val.className = 'arch-stat-val';
    val.textContent = row[1];
    el.appendChild(label);
    el.appendChild(val);
    body.appendChild(el);
  });

  // 12-Factor scorecard
  var scoreData = score12Factors(state);
  var scoreDiv = document.createElement('div');
  scoreDiv.className = 'arch-score';
  var scoreHeader = document.createElement('div');
  scoreHeader.className = 'arch-score__header';
  scoreHeader.textContent = '12-Factor Score: ' + scoreData.score + '/12';
  scoreDiv.appendChild(scoreHeader);

  scoreData.results.forEach(function(r) {
    var row = document.createElement('div');
    row.className = 'arch-factor';
    var icon = document.createElement('span');
    icon.className = 'arch-factor__icon ' + (r.pass ? 'pass' : 'fail');
    icon.textContent = r.pass ? '✓' : '✗';
    var name = document.createElement('span');
    name.className = 'arch-factor__name' + (r.pass ? ' pass' : '');
    name.textContent = r.name;
    row.appendChild(icon);
    row.appendChild(name);
    scoreDiv.appendChild(row);
  });

  body.appendChild(scoreDiv);
}
