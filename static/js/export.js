// Export the campaign as JSON (with architecture metadata) or Markdown.
'use strict';

// ---- Export ----
$('#btn-export-json').addEventListener('click', async () => {
  if (!code) return;
  try {
    const res = await fetch(BASE + '/' + code + '/export');
    const data = await res.json();

    // Enrich with transparency sections
    var scoreData = campaignState ? score12Factors(campaignState) : null;
    var enriched = {
      _meta: {
        format: 'Agentic D&D Campaign Export',
        version: '2.0',
        description: 'Full campaign log with agent architecture metadata. Each turn includes tool calls, reasoning, token usage, and context estimates.',
        exported_at: new Date().toISOString(),
      },
      _architecture: {
        pattern: 'multi-agent-turn-based',
        agent_count: (data.agents || []).length,
        dm_tools: ['roll_dice','create_npc','lookup_rule','view_npc_sheet','set_scene','resolve_action','remember_fact','recall_facts','take_note','read_notes'],
        player_tools: ['roll_dice','take_note','read_notes'],
        twelve_factor_score: scoreData ? (scoreData.score + '/12') : 'N/A',
        twelve_factor_results: scoreData ? scoreData.results : [],
      },
    };
    if (campaignState && campaignState.rulebook_name) {
      enriched._rag_config = {
        rulebook: campaignState.rulebook_name,
        chunks: campaignState.rulebook_chunks || 0,
        search_method: 'keyword + title matching',
        top_k: 3,
      };
    }
    // Merge enriched sections with original data
    var exported = Object.assign(enriched, data);
    downloadBlob(JSON.stringify(exported, null, 2), 'campaign-' + code + '.json', 'application/json');
    toast('JSON exported');
  } catch (e) { toast('Export failed'); }
});

$('#btn-export-md').addEventListener('click', async () => {
  if (!code) return;
  try {
    const res = await fetch(BASE + '/' + code + '/export');
    const data = await res.json();
    const md = buildMarkdownExport(data);
    downloadBlob(md, 'campaign-' + code + '.md', 'text/markdown');
    toast('Markdown exported');
  } catch (e) { toast('Export failed'); }
});

function downloadBlob(content, filename, type) {
  const blob = new Blob([content], { type: type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function buildMarkdownExport(data) {
  let md = '# ' + (data.campaign || 'Campaign') + '\n\n';
  md += '**Code:** ' + (data.code || '') + '  \n';
  md += '**Rounds:** ' + (data.total_rounds || 0) + '  \n';
  md += '**Agents:** ' + (data.agents || []).map(a => a.name + ' (' + a.role + ')').join(', ') + '\n\n';

  // Architecture overview
  md += '## Architecture\n\n';
  md += '- **Pattern:** Multi-agent (turn-based)\n';
  md += '- **Agents:** ' + (data.agents || []).length + '\n';
  var dmAgent = (data.agents || []).find(a => a.role === 'dm');
  if (dmAgent) md += '- **DM Tools:** ' + (dmAgent.tools || []).join(', ') + '\n';
  var playerAgent = (data.agents || []).find(a => a.role === 'player');
  if (playerAgent) md += '- **Player Tools:** ' + (playerAgent.tools || []).join(', ') + '\n';
  if (campaignState && campaignState.rulebook_name) {
    md += '- **Rulebook:** ' + campaignState.rulebook_name + ' (' + (campaignState.rulebook_chunks || 0) + ' chunks)\n';
  }

  // 12-factor score
  if (campaignState) {
    var scoreData = score12Factors(campaignState);
    md += '\n### 12-Factor Score: ' + scoreData.score + '/12\n\n';
    scoreData.results.forEach(function(r) {
      md += (r.pass ? '- [x] ' : '- [ ] ') + r.name + '\n';
    });
  }

  md += '\n---\n\n## Campaign Log\n\n';

  (data.turns || []).forEach(turn => {
    const agent = (data.agents || []).find(a => a.id === turn.agent_id) || {};
    md += '### ' + (agent.name || turn.agent_id) + ' — Round ' + turn.round + '\n\n';
    md += (turn.message || '') + '\n\n';

    if (turn.whispers_received && turn.whispers_received.length) {
      turn.whispers_received.forEach(w => {
        md += '> *Whisper: "' + (w.text || '') + '"*\n\n';
      });
    }

    if (turn.tool_calls && turn.tool_calls.length) {
      md += '<details><summary>Tool calls (' + turn.tool_calls.length + ')</summary>\n\n';
      turn.tool_calls.forEach(tc => {
        md += '- `' + tc.name + '(' + JSON.stringify(tc.args) + ')` → ' + JSON.stringify(tc.result) + '\n';
      });
      md += '\n</details>\n\n';
    }

    if (turn.tokens_in || turn.tokens_out) {
      md += '*Tokens: ' + (turn.tokens_in || 0) + ' in / ' + (turn.tokens_out || 0) + ' out*\n\n';
    }

    md += '---\n\n';
  });

  if (data.facts && Object.keys(data.facts).length > 0) {
    md += '## World Facts\n\n';
    Object.entries(data.facts).forEach(([fid, fact]) => {
      md += '- **' + (fact.category || 'fact') + '**: ' + (fact.fact || fact.text || fact.content || JSON.stringify(fact)) + '\n';
    });
    md += '\n';
  }

  // Agent system prompts
  if (data.agents && data.agents.some(a => a.system_prompt)) {
    md += '## Agent System Prompts\n\n';
    data.agents.forEach(function(a) {
      if (a.system_prompt) {
        md += '### ' + a.name + ' (' + a.role + ')\n\n';
        md += '```markdown\n' + a.system_prompt + '\n```\n\n';
      }
    });
  }

  return md;
}
