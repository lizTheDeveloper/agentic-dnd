// Memory panel: the world facts the DM has stored, filterable by category.
'use strict';

// ---- Memory Panel ----
function renderMemory(state) {
  const facts = state.facts || {};
  const entries = Object.entries(facts);
  $('#memory-count').textContent = '(' + entries.length + ')';

  // Build filter buttons
  const filterEl = $('#memory-filter');
  if (filterEl.children.length === 0) {
    FACT_CATEGORIES.forEach(cat => {
      const btn = document.createElement('button');
      btn.textContent = cat;
      btn.classList.toggle('active', cat === activeMemoryFilter);
      btn.addEventListener('click', () => {
        activeMemoryFilter = cat;
        filterEl.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.textContent === cat));
        renderMemoryList(entries);
      });
      filterEl.appendChild(btn);
    });
  }

  renderMemoryList(entries);
}

function renderMemoryList(entries) {
  const list = $('#memory-list');
  list.innerHTML = '';
  if (memoryCollapsed) {
    list.classList.add('collapsed');
    return;
  }
  list.classList.remove('collapsed');

  entries.forEach(([fid, fact]) => {
    const cat = (fact.category || 'world').toLowerCase();
    if (activeMemoryFilter !== 'all' && cat !== activeMemoryFilter) return;

    const item = document.createElement('div');
    item.className = 'fact-item';
    item.dataset.cat = cat;

    const catDiv = document.createElement('div');
    catDiv.className = 'fact-item__cat';
    catDiv.textContent = cat;

    const textDiv = document.createElement('div');
    textDiv.className = 'fact-item__text';
    textDiv.textContent = fact.fact || fact.text || fact.content || JSON.stringify(fact);

    const roundDiv = document.createElement('div');
    roundDiv.className = 'fact-item__round';
    roundDiv.textContent = fact.round ? 'Round ' + fact.round : '';

    item.appendChild(catDiv);
    item.appendChild(textDiv);
    item.appendChild(roundDiv);
    list.appendChild(item);
  });

  if (list.children.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'fact-item__text';
    empty.style.padding = '0.5rem';
    empty.textContent = 'No facts extracted yet.';
    list.appendChild(empty);
  }
}
