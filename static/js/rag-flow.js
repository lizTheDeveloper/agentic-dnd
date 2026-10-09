// RAG pipeline view for lookup_rule calls: query -> chunk store ->
// keyword search -> matched chunks with scores.  It parses the result text
// that retrieval.py's lookup_rule() returns.
'use strict';

// Build RAG pipeline visualization for lookup_rule
function buildRagFlow(tc, state) {
  var flow = document.createElement('div');
  flow.className = 'rag-flow';

  var query = (tc.args && tc.args.query) || '';
  var resultText = tc.result || '';
  var chunkCount = state.rulebook_chunks || 0;
  var rulebookName = state.rulebook_name || 'Rulebook';

  // Step 1: Query
  flow.appendChild(buildRagStep('Query', '"' + query + '"', true));

  // Step 2: Chunks
  flow.appendChild(buildRagStep('Chunk store', chunkCount + ' chunks from ' + rulebookName, true));

  // Step 3: Search + results
  var chunkMatches = resultText.split(/\n---\s/).slice(1);
  if (chunkMatches.length > 0) {
    var searchStep = buildRagStep('Keyword search', 'Matched ' + chunkMatches.length + ' chunk' + (chunkMatches.length !== 1 ? 's' : ''), true);

    // Parse and show matched chunks
    chunkMatches.forEach(function(chunkText) {
      var titleMatch = chunkText.match(/^(.+?)\s*\(relevance:\s*(\d+)\)\s*---\n?([\s\S]*)/);
      if (titleMatch) {
        var resultCard = document.createElement('div');
        resultCard.className = 'rag-result-card';
        var titleEl = document.createElement('span');
        titleEl.className = 'rag-result-title';
        titleEl.textContent = titleMatch[1].trim();
        var scoreEl = document.createElement('span');
        scoreEl.className = 'rag-result-score';
        scoreEl.textContent = 'score: ' + titleMatch[2];
        var previewEl = document.createElement('div');
        previewEl.className = 'rag-result-preview';
        var ct = titleMatch[3].trim();
        previewEl.textContent = ct.length > 120 ? ct.substring(0, 117) + '...' : ct;
        resultCard.appendChild(titleEl);
        resultCard.appendChild(scoreEl);
        resultCard.appendChild(previewEl);
        searchStep.querySelector('.rag-step__content').appendChild(resultCard);
      }
    });
    flow.appendChild(searchStep);
  } else if (resultText.includes('No matching rules') || resultText.includes('No rulebook')) {
    flow.appendChild(buildRagStep('Search', 'No matching rules found — DM adjudicates', false));
  }

  // Step 4: Used in narration
  flow.appendChild(buildRagStep('Result', chunkMatches.length > 0 ? 'Top result used in narration' : 'DM adjudicated without rules', false));

  return flow;
}

function buildRagStep(label, detail, hasLine) {
  var step = document.createElement('div');
  step.className = 'rag-step';

  var pipe = document.createElement('div');
  pipe.className = 'rag-step__pipe';
  var dot = document.createElement('div');
  dot.className = 'rag-step__dot';
  pipe.appendChild(dot);
  if (hasLine) {
    var line = document.createElement('div');
    line.className = 'rag-step__line';
    pipe.appendChild(line);
  }

  var content = document.createElement('div');
  content.className = 'rag-step__content';
  var labelEl = document.createElement('div');
  labelEl.className = 'rag-step__label';
  labelEl.textContent = label;
  content.appendChild(labelEl);
  if (detail) {
    var detailEl = document.createElement('div');
    detailEl.className = 'rag-step__detail';
    detailEl.textContent = detail;
    content.appendChild(detailEl);
  }

  step.appendChild(pipe);
  step.appendChild(content);
  return step;
}
