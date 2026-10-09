// "How this turn worked": the expandable detail under each turn card.
// Reasoning, the exact prompt the agent saw, tool calls, memory writes,
// token usage, and a raw JSON inspector.
'use strict';

// Render the Level 1 detail body (lazy)
function renderTurnDetail(container, entry, state) {
  // Chain of thought
  if (entry.reasoning && entry.reasoning.length > 20) {
    var sec = document.createElement('div');
    sec.className = 'td-section';
    var hdr = document.createElement('div');
    hdr.className = 'td-section__header';
    hdr.innerHTML = '<span class="td-section__header-icon">&#129504;</span> What the agent was thinking';
    sec.appendChild(hdr);
    var content = document.createElement('div');
    content.className = 'monologue-content';
    content.textContent = entry.reasoning;
    sec.appendChild(content);
    container.appendChild(sec);
  }

  // Prompt context — "What the agent saw"
  if (entry.prompt_context) {
    var sec = document.createElement('div');
    sec.className = 'td-section';
    var hdr = document.createElement('div');
    hdr.className = 'td-section__header';
    hdr.innerHTML = '<span class="td-section__header-icon">&#128220;</span> What the agent saw';
    sec.appendChild(hdr);
    var pcDiv = document.createElement('div');
    pcDiv.className = 'prompt-context';
    var pc = entry.prompt_context;
    if (pc.system_prompt) {
      var sysLen = Math.ceil(pc.system_prompt.length / 4);
      pcDiv.appendChild(buildPromptSection('System prompt', '~' + fmtNum(sysLen) + ' tokens', function(body) {
        var pre = document.createElement('div');
        pre.className = 'prompt-section__pre';
        pre.textContent = pc.system_prompt;
        body.appendChild(pre);
      }));
    }
    if (pc.messages && pc.messages.length > 0) {
      pcDiv.appendChild(buildPromptSection('Messages (' + pc.messages.length + ')', null, function(body) {
        pc.messages.forEach(function(m) {
          var msgEl = document.createElement('div');
          var role = m.role || 'user';
          msgEl.className = 'prompt-msg role-' + role;
          var roleEl = document.createElement('div');
          roleEl.className = 'prompt-msg__role';
          roleEl.textContent = role;
          msgEl.appendChild(roleEl);
          var contentEl = document.createElement('div');
          contentEl.className = 'prompt-msg__content';
          var text = m.content || '';
          if (!text && m.tool_calls) {
            text = '[tool_calls: ' + m.tool_calls.map(function(tc) { return tc.function ? tc.function.name : '?'; }).join(', ') + ']';
          }
          contentEl.textContent = text.length > 2000 ? text.substring(0, 2000) + '\n... (' + text.length + ' chars total)' : text;
          msgEl.appendChild(contentEl);
          body.appendChild(msgEl);
        });
      }));
    }
    if (pc.tools && pc.tools.length > 0) {
      pcDiv.appendChild(buildPromptSection('Tools available (' + pc.tools.length + ')', null, function(body) {
        var toolsDiv = document.createElement('div');
        toolsDiv.className = 'prompt-tools-list';
        pc.tools.forEach(function(t) {
          var tag = document.createElement('span');
          tag.className = 'prompt-tool-tag';
          tag.textContent = t;
          toolsDiv.appendChild(tag);
        });
        body.appendChild(toolsDiv);
      }));
    }
    sec.appendChild(pcDiv);
    container.appendChild(sec);
  }

  // Tool calls — enhanced detail
  if (entry.tool_calls && entry.tool_calls.length > 0) {
    var sec = document.createElement('div');
    sec.className = 'td-section';
    var hdr = document.createElement('div');
    hdr.className = 'td-section__header';
    hdr.innerHTML = '<span class="td-section__header-icon">&#128295;</span> Tools called (' + entry.tool_calls.length + ')';
    sec.appendChild(hdr);

    entry.tool_calls.forEach(function(tc) {
      if (tc.name === 'lookup_rule') {
        // RAG pipeline visualization
        sec.appendChild(buildRagFlow(tc, state));
      } else {
        sec.appendChild(buildToolDetail(tc));
      }
    });
    container.appendChild(sec);
  }

  // Memory changes — find remember_fact calls
  var memCalls = (entry.tool_calls || []).filter(function(tc) {
    return tc.name === 'remember_fact';
  });
  if (memCalls.length > 0) {
    var sec = document.createElement('div');
    sec.className = 'td-section';
    var hdr = document.createElement('div');
    hdr.className = 'td-section__header';
    hdr.innerHTML = '<span class="td-section__header-icon">&#128190;</span> Memory changes';
    sec.appendChild(hdr);

    memCalls.forEach(function(tc) {
      var change = document.createElement('div');
      change.className = 'mem-change added';
      var factText = (tc.args && tc.args.fact) || '';
      var cat = (tc.args && tc.args.category) || 'world';
      change.textContent = '+ "' + factText + '" (' + cat + ')';
      sec.appendChild(change);
    });
    container.appendChild(sec);
  }

  // Token usage
  if (entry.tokens_in > 0 || entry.tokens_out > 0) {
    var sec = document.createElement('div');
    sec.className = 'td-section';
    var hdr = document.createElement('div');
    hdr.className = 'td-section__header';
    hdr.innerHTML = '<span class="td-section__header-icon">&#128202;</span> Token usage';
    sec.appendChild(hdr);

    var meta = document.createElement('div');
    meta.className = 'meta-content';
    var parts = [];
    parts.push('Input: ' + fmtNum(entry.tokens_in || 0) + ' tokens');
    parts.push('Output: ' + fmtNum(entry.tokens_out || 0) + ' tokens');
    if (entry.tokens_in || entry.tokens_out) {
      var costCents = ((entry.tokens_in || 0) / 1000000) * 100 + ((entry.tokens_out || 0) / 1000000) * 300;
      parts.push('Est. cost: $' + (costCents / 100).toFixed(4));
    }

    // Context fill bar
    if (entry.context_estimate && entry.context_estimate.total) {
      var pct = Math.round((entry.context_estimate.total / 32000) * 100);
      parts.push('Context fill: ' + pct + '%');
    }

    meta.textContent = parts.join(' | ');
    sec.appendChild(meta);

    // Context breakdown bars if available
    if (entry.context_estimate && entry.context_estimate.breakdown) {
      var bd = entry.context_estimate.breakdown;
      var cats = [
        { key: 'system', label: 'System', color: 'system' },
        { key: 'tools', label: 'Tools', color: 'tools' },
        { key: 'history', label: 'History', color: 'history' },
        { key: 'facts', label: 'Facts', color: 'facts' },
        { key: 'notes', label: 'Notes', color: 'notes' },
      ];
      cats.forEach(function(cat) {
        var val = bd[cat.key] || 0;
        if (val === 0) return;
        var pct = Math.round((val / 32000) * 100);
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
        sec.appendChild(row);
      });
    }
    container.appendChild(sec);
  }

  // ---- Level 2: JSON Inspector ----
  var jsonTrigger = document.createElement('span');
  jsonTrigger.className = 'json-trigger';
  jsonTrigger.textContent = 'View JSON';
  var jsonPanel = document.createElement('div');
  jsonPanel.className = 'json-inspector';

  jsonTrigger.addEventListener('click', function() {
    var isOpen = jsonPanel.classList.toggle('open');
    if (isOpen && !jsonPanel.dataset.rendered) {
      jsonPanel.innerHTML = syntaxHighlight(JSON.stringify(entry, null, 2));
      jsonPanel.dataset.rendered = '1';
    }
  });

  container.appendChild(jsonTrigger);
  container.appendChild(jsonPanel);
}

function buildPromptSection(label, meta, renderFn) {
  var section = document.createElement('div');
  section.className = 'prompt-section';
  var trigger = document.createElement('div');
  trigger.className = 'prompt-section__trigger';
  var labelSpan = document.createElement('span');
  labelSpan.className = 'prompt-section__label';
  labelSpan.textContent = label;
  trigger.appendChild(labelSpan);
  if (meta) {
    var metaSpan = document.createElement('span');
    metaSpan.className = 'prompt-section__meta';
    metaSpan.textContent = meta;
    trigger.appendChild(metaSpan);
  }
  var body = document.createElement('div');
  body.className = 'prompt-section__body';
  var rendered = false;
  trigger.addEventListener('click', function() {
    var isOpen = body.classList.toggle('open');
    trigger.classList.toggle('open', isOpen);
    if (isOpen && !rendered) { renderFn(body); rendered = true; }
  });
  section.appendChild(trigger);
  section.appendChild(body);
  return section;
}

// Build enhanced tool detail card
function buildToolDetail(tc) {
  var el = document.createElement('div');
  el.className = 'tool-detail';
  if (tc.name) el.dataset.tool = tc.name;

  var nameRow = document.createElement('div');
  nameRow.className = 'tool-detail__row';
  var nameLabel = document.createElement('span');
  nameLabel.className = 'tool-detail__label';
  nameLabel.textContent = 'Tool';
  var nameVal = document.createElement('span');
  nameVal.className = 'tool-detail__val';
  nameVal.style.fontWeight = '700';
  nameVal.textContent = tc.name;
  nameRow.appendChild(nameLabel);
  nameRow.appendChild(nameVal);
  el.appendChild(nameRow);

  // Args
  if (tc.args && Object.keys(tc.args).length > 0) {
    Object.entries(tc.args).forEach(function(pair) {
      var row = document.createElement('div');
      row.className = 'tool-detail__row tool-detail__connector';
      var label = document.createElement('span');
      label.className = 'tool-detail__label';
      label.textContent = pair[0];
      var val = document.createElement('span');
      val.className = 'tool-detail__val';
      val.textContent = JSON.stringify(pair[1]);
      row.appendChild(label);
      row.appendChild(val);
      el.appendChild(row);
    });
  }

  // Result
  var resultRow = document.createElement('div');
  resultRow.className = 'tool-detail__row';
  resultRow.style.marginTop = '0.2rem';
  resultRow.style.paddingTop = '0.2rem';
  resultRow.style.borderTop = '1px solid var(--border-subtle)';
  var resultLabel = document.createElement('span');
  resultLabel.className = 'tool-detail__label';
  resultLabel.textContent = 'Result';
  var resultVal = document.createElement('span');
  resultVal.className = 'tool-detail__val';
  resultVal.appendChild(formatResult(tc));
  resultRow.appendChild(resultLabel);
  resultRow.appendChild(resultVal);
  el.appendChild(resultRow);

  return el;
}
