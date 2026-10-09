// The four rulebook chunking strategies.  Pure functions, no DOM.
//
// Each returns [{id, title, content, tokens_est}].  These chunks are what
// gets POSTed to /create and stored in Redis; retrieval.py searches them
// when the DM calls lookup_rule.  tokens_est is the usual chars / 4.
//
//   chunkHeader     split on markdown headings (#..####)
//   chunkParagraph  split on blank lines
//   chunkFixed      every N chars, nudged back to a sentence/line break
//   chunkSemantic   header split, then paragraph-split sections > 800 tokens
'use strict';

function chunkHeader(text) {
  var lines = text.split('\n');
  var chunks = [], current = { title: '', lines: [] };
  for (var i = 0; i < lines.length; i++) {
    var m = lines[i].match(/^(#{1,4})\s+(.+)/);
    if (m) {
      if (current.lines.length > 0 || current.title) {
        var content = current.lines.join('\n').trim();
        if (content) chunks.push({ title: current.title || 'Preamble', content: content });
      }
      current = { title: m[2].trim(), lines: [] };
    } else {
      current.lines.push(lines[i]);
    }
  }
  if (current.lines.length > 0) {
    var c = current.lines.join('\n').trim();
    if (c) chunks.push({ title: current.title || 'Preamble', content: c });
  }
  return chunks.map(function(c, i) {
    return { id: 'chunk-' + String(i+1).padStart(2,'0'), title: c.title, content: c.content, tokens_est: Math.ceil(c.content.length / 4) };
  });
}
function chunkParagraph(text) {
  var paras = text.split(/\n\n+/), chunks = [];
  for (var i = 0; i < paras.length; i++) {
    var p = paras[i].trim();
    if (!p || p.length < 10) continue;
    var tm = p.match(/^#{1,4}\s+(.+)/);
    var title = tm ? tm[1].trim() : p.substring(0,60).replace(/\n/g,' ');
    if (title.length >= 60) title = title.substring(0,57) + '...';
    chunks.push({ id: 'chunk-' + String(chunks.length+1).padStart(2,'0'), title: title, content: p, tokens_est: Math.ceil(p.length/4) });
  }
  return chunks;
}
function chunkFixed(text, size) {
  var chunks = [], pos = 0;
  while (pos < text.length) {
    var end = Math.min(pos + size, text.length);
    if (end < text.length) {
      var ss = Math.max(pos, end - Math.floor(size * 0.2));
      for (var j = end; j >= ss; j--) {
        if (text[j] === '.' || text[j] === '\n') { end = j + 1; break; }
      }
    }
    var content = text.substring(pos, end).trim();
    if (content) {
      var tl = content.split('\n')[0].substring(0,60);
      if (tl.length >= 60) tl = tl.substring(0,57) + '...';
      chunks.push({ id: 'chunk-' + String(chunks.length+1).padStart(2,'0'), title: tl, content: content, tokens_est: Math.ceil(content.length/4) });
    }
    pos = end;
  }
  return chunks;
}
function chunkSemantic(text) {
  var headerChunks = chunkHeader(text), result = [];
  for (var i = 0; i < headerChunks.length; i++) {
    if (headerChunks[i].tokens_est > 800) {
      var subs = headerChunks[i].content.split(/\n\n+/);
      for (var j = 0; j < subs.length; j++) {
        var s = subs[j].trim();
        if (!s || s.length < 10) continue;
        var st = s.match(/^#{1,4}\s+(.+)/);
        result.push({ id: 'chunk-' + String(result.length+1).padStart(2,'0'), title: st ? st[1].trim() : headerChunks[i].title + ' (part ' + (j+1) + ')', content: s, tokens_est: Math.ceil(s.length/4) });
      }
    } else {
      headerChunks[i].id = 'chunk-' + String(result.length+1).padStart(2,'0');
      result.push(headerChunks[i]);
    }
  }
  return result;
}
function doChunk(text, strategy, fixedSize) {
  if (!text || !text.trim()) return [];
  switch(strategy) {
    case 'paragraph': return chunkParagraph(text);
    case 'fixed': return chunkFixed(text, fixedSize || 500);
    case 'semantic': return chunkSemantic(text);
    default: return chunkHeader(text);
  }
}
function chunkStats(chunks) {
  if (!chunks.length) return { count:0, min:0, max:0, avg:0, median:0 };
  var t = chunks.map(function(c){return c.tokens_est;}).sort(function(a,b){return a-b;});
  var sum = t.reduce(function(a,b){return a+b;},0);
  var mid = Math.floor(t.length/2);
  return { count:t.length, min:t[0], max:t[t.length-1], avg:Math.round(sum/t.length), median:t.length%2?t[mid]:Math.round((t[mid-1]+t[mid])/2) };
}
