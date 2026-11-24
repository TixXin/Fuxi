// 浮汐新增（不属于上游）：在可终止的线程里用 Readability 抽网页正文，避免拖住内核的消息事件循环。
// 输入 workerData {html, url}，输出 {title, description, site, byline, article} 或 {error}。
'use strict';
const { parentPort, workerData } = require('node:worker_threads');
const { parseHTML } = require('linkedom');
const { Readability } = require('@mozilla/readability');
const { parsePage, decodeEntities } = require('../src/adapters/links');

const BLOCK = new Set(['P', 'DIV', 'SECTION', 'ARTICLE', 'BLOCKQUOTE', 'PRE', 'UL', 'OL', 'LI', 'TABLE', 'TR',
  'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BR', 'HR', 'FIGURE', 'FIGCAPTION', 'DL', 'DT', 'DD']);
const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'IFRAME', 'BUTTON', 'FORM']);

/** 把正文节点转成保留段落换行的纯文本 */
function toText(root) {
  const out = [];
  const walk = (node) => {
    if (node.nodeType === 3) { out.push(node.textContent); return; }
    if (node.nodeType !== 1 || SKIP.has(node.tagName)) return;
    const block = BLOCK.has(node.tagName);
    if (block) out.push('\n');
    if (node.tagName === 'LI') out.push('- ');
    for (const child of node.childNodes) walk(child);
    if (block) out.push('\n');
  };
  walk(root);
  return out.join('')
    .split('\n')
    .map((line) => line.replace(/[ \t 　]+/g, ' ').trim())
    .filter((line) => line && line !== '-')
    .join('\n');
}

function extract(html, url) {
  const { document } = parseHTML(html);
  const article = new Readability(document, { charThreshold: 200, serializer: (el) => el }).parse();
  if (!article || !article.content) return null;
  return {
    title: String(article.title || '').trim(),
    description: decodeEntities(String(article.excerpt || '')).trim(),
    site: String(article.siteName || '').trim(),
    byline: String(article.byline || '').trim(),
    article: toText(article.content),
    url,
  };
}

let result;
try {
  result = extract(workerData.html, workerData.url);
} catch {
  result = null;
}
if (!result || result.article.length < 80) {
  // Readability 没认出正文：退回内核原来的抽法（og 标题/简介 + 段落）
  const fallback = parsePage(workerData.html, workerData.url);
  result = {
    title: (result && result.title) || fallback.title,
    description: (result && result.description) || fallback.description,
    site: (result && result.site) || '',
    byline: '',
    article: fallback.article,
    url: workerData.url,
  };
}
parentPort.postMessage(result);
