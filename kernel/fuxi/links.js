// 浮汐新增（不属于上游）：更好的链接阅读。由 preload.js 调 install() 替换上游
// src/adapters/links.js 导出的 describeLink / formatForPrompt（内核都是按 links.xxx 调用，换掉导出即可，
// 不改上游文件）。
//
// - 普通网页：仍用上游 fetchUrl（公网限定、防 DNS 重绑定、大小与总时长限制），正文改用 Readability
//   在可终止线程里抽取，保留段落；Readability 认不出时退回上游的抽法。
// - GitHub issue / PR / release：走公开 API 拿标题、状态和正文（网页版抽出来全是导航和时间线噪音）。
// - B 站视频：走公开 web API（不登录）拿标题、UP 主、完整简介、时长、播放点赞和分 P；有字幕时附上字幕节选
//   （不登录时字幕常常是空的）。b23.tv 短链先解析跳转。API 失败时交回上游。
// - 其它短视频 / 笔记（抖音、小红书……）：交回上游原逻辑，只读标题和文案。
//
// 上游 persona/prompt.js 的 linkBlock 会把整段链接说明截到 1200 字，所以这里把输出控制在这个预算内，
// 并在正文被截断时明确告诉模型「只是开头」。
'use strict';
const path = require('node:path');
const { Worker } = require('node:worker_threads');
const links = require('../src/adapters/links');

const upstream = { describeLink: links.describeLink, formatForPrompt: links.formatForPrompt };

const PROMPT_BUDGET = 1150;
const MAX_ARTICLE = 8000;
const MAX_PARSERS = 2;
const SHORT_KINDS = new Set(['video', 'note']);

const REASONS = {
  timeout: '读取超时',
  aborted: '读取被中断',
  private_host: '这是内网或本机地址，不读',
  too_large: '页面太大',
  not_html: '不是网页（可能是图片、PDF 或文件）',
  bad_url: '链接格式不对',
  bad_protocol: '只支持 http / https 链接',
  ENOTFOUND: '域名解析不到',
  ECONNREFUSED: '网站拒绝连接',
  ECONNRESET: '连接被网站断开',
  parse_timeout: '页面太复杂，解析超时',
  parse_failed: '页面解析失败',
  parse_busy: '同时在读的链接太多',
  empty: '页面里没有可读的正文（可能要登录，或者内容要靠脚本加载）',
};

function reasonText(code) {
  const c = String(code || '');
  const http = c.match(/^http_(\d{3})$/);
  if (http) {
    const s = Number(http[1]);
    if (s === 404 || s === 410) return `页面不存在（${s}）`;
    if (s === 401 || s === 403) return `网站拒绝访问（${s}）`;
    if (s === 429) return '网站限流（429）';
    if (s >= 500) return `网站出错（${s}）`;
    return `网站返回 ${s}`;
  }
  return REASONS[c] || `原因不明（${c || 'unknown'}）`;
}

const fail = (url, error) => ({ ok: false, source: 'fuxi', url, kind: links.kindOf(url), error });

// ---------- GitHub ----------

const NAME = /^[A-Za-z0-9_.-]+$/;

/** 识别 GitHub issue / PR / release 链接，返回对应的 API 地址；其它返回 null */
function githubTarget(urlString) {
  let u;
  try { u = new URL(urlString); } catch { return null; }
  if (u.hostname !== 'github.com' && u.hostname !== 'www.github.com') return null;
  const [owner, repo, type, ...rest] = u.pathname.split('/').filter(Boolean);
  if (!owner || !repo || !NAME.test(owner) || !NAME.test(repo)) return null;
  const base = `https://api.github.com/repos/${owner}/${repo}`;
  if ((type === 'issues' || type === 'pull') && /^\d+$/.test(rest[0] || '')) {
    return { owner, repo, kind: type === 'pull' ? 'pr' : 'issue', number: rest[0], api: `${base}/issues/${rest[0]}` };
  }
  if (type === 'releases' && rest[0] === 'tag' && rest[1]) {
    const tag = decodeURIComponent(rest.slice(1).join('/'));
    return { owner, repo, kind: 'release', tag, api: `${base}/releases/tags/${encodeURIComponent(tag)}` };
  }
  return null;
}

function cleanMarkdown(s) {
  return String(s || '')
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\r\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

const day = (iso) => (iso ? String(iso).slice(0, 10) : '');

/** 把 API 返回的 JSON 转成链接信息；结构不对返回 null */
function githubInfo(target, data, url) {
  if (!data || typeof data !== 'object') return null;
  const repo = `${target.owner}/${target.repo}`;
  if (target.kind === 'release') {
    if (!data.tag_name) return null;
    return {
      title: `${repo} 版本 ${data.name || data.tag_name}`,
      meta: [`标签 ${data.tag_name}`, data.published_at && `发布于 ${day(data.published_at)}`,
        data.prerelease && '预发布版'].filter(Boolean).join(' · '),
      article: cleanMarkdown(data.body),
    };
  }
  if (!data.title || !data.number) return null;
  const isPr = Boolean(data.pull_request);
  let state = data.state === 'open' ? '未关闭' : '已关闭';
  if (isPr && data.pull_request.merged_at) state = `已合并（${day(data.pull_request.merged_at)}）`;
  else if (!isPr && data.state === 'closed' && data.state_reason === 'completed') state = '已解决关闭';
  else if (!isPr && data.state_reason === 'not_planned') state = '关闭（不计划处理）';
  const labels = (data.labels || []).map((l) => (typeof l === 'string' ? l : l && l.name)).filter(Boolean);
  return {
    title: `${repo} ${isPr ? 'PR' : 'Issue'} #${data.number}：${data.title}`,
    meta: [`状态：${state}`, data.user && data.user.login && `发起人 ${data.user.login}`,
      data.created_at && `创建于 ${day(data.created_at)}`, labels.length && `标签 ${labels.join('、')}`,
      `评论 ${Number(data.comments) || 0} 条`].filter(Boolean).join(' · '),
    article: cleanMarkdown(data.body) || '（没有正文描述）',
    url,
  };
}

async function readGithub(target, url, opts) {
  const res = await links.fetchUrl(target.api, { ...opts, maxBytes: 1024 * 1024 });
  if (!res.ok || res.status !== 200) return null; // 限流、私有仓库等：退回读网页
  let data;
  try { data = JSON.parse(res.body); } catch { return null; }
  const info = githubInfo(target, data, url);
  if (!info) return null;
  return { ok: true, source: 'fuxi', url, kind: 'github', site: 'GitHub', description: '', byline: '', ...info,
    article: info.article.slice(0, MAX_ARTICLE), fullLength: info.article.length };
}

// ---------- B 站 ----------

const BILI_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
  + 'Chrome/124.0 Safari/537.36';
const BILI_HEADERS = { 'User-Agent': BILI_UA, Referer: 'https://www.bilibili.com/', Accept: 'application/json, text/plain, */*' };
const BILI_HOSTS = /^(?:www\.|m\.)?bilibili\.com$/i;
const BILI_SHORT = /^(?:b23\.tv|bili2233\.cn)$/i;
const BILI_SUB_HOSTS = /(?:^|\.)(?:hdslb\.com|bilibili\.com|biliapi\.net)$/i;
const MAX_DESC = 2000;
const MAX_SUBTITLE = 6000;

/** 认出 B 站视频链接：{ bvid | aid, page }；b23.tv 短链返回 { short: true }；其它返回 null */
function bilibiliTarget(urlString) {
  let u;
  try { u = new URL(urlString); } catch { return null; }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
  if (BILI_SHORT.test(u.hostname)) return { short: true };
  if (!BILI_HOSTS.test(u.hostname)) return null;
  const m = u.pathname.match(/^\/(?:s\/)?video\/(?:(BV[0-9A-Za-z]{10})|av(\d{1,15}))(?:\/|$)/i);
  if (!m) return null;
  const page = Math.max(1, Number.parseInt(u.searchParams.get('p'), 10) || 1);
  if (m[1]) return { bvid: 'BV' + m[1].slice(2), page };
  return { aid: m[2], page };
}

function remainingMs(deadline) { return Math.max(1, deadline - Date.now()); }

/** 带总截止时间和外部中断的 fetch；超时/中断抛错 */
async function biliFetch(url, { deadline, signal, redirect = 'follow' }) {
  const signals = [AbortSignal.timeout(remainingMs(deadline))];
  if (signal) signals.push(signal);
  return globalThis.fetch(url, { headers: BILI_HEADERS, redirect, signal: AbortSignal.any(signals) });
}

async function biliJson(url, opts) {
  const res = await biliFetch(url, opts);
  if (!res.ok) throw new Error(`http_${res.status}`);
  return res.json();
}

/** b23.tv 短链：只看 Location，不下载落地页；最多跳 3 次 */
async function resolveBiliShort(urlString, opts) {
  let current = urlString;
  for (let i = 0; i < 3; i += 1) {
    const res = await biliFetch(current, { ...opts, redirect: 'manual' });
    const loc = res.status >= 300 && res.status < 400 && res.headers.get('location');
    if (res.body && typeof res.body.cancel === 'function') res.body.cancel().catch(() => {});
    if (!loc) return null;
    current = new URL(loc, current).toString();
    const t = bilibiliTarget(current);
    if (!t) return null; // 跳到了直播、番剧、专栏等：不归这里管
    if (!t.short) return { target: t, url: current };
  }
  return null;
}

function biliDuration(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (!s) return '';
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(h ? 2 : 1, '0');
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function biliCount(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v < 0) return '';
  if (v >= 1e8) return `${(v / 1e8).toFixed(1).replace(/\.0$/, '')} 亿`;
  if (v >= 1e4) return `${(v / 1e4).toFixed(1).replace(/\.0$/, '')} 万`;
  return String(v);
}

function biliDate(ts) {
  const v = Number(ts);
  if (!v) return '';
  // B 站发布时间按北京时间算
  return new Date(v * 1000 + 8 * 3600 * 1000).toISOString().slice(0, 10);
}

/** view 接口的 data → 链接信息（不含字幕）；结构不对返回 null */
function bilibiliInfo(data, target, url) {
  if (!data || typeof data !== 'object' || !data.title || !data.cid) return null;
  const pages = Array.isArray(data.pages) ? data.pages : [];
  const page = pages.find((p) => Number(p.page) === target.page) || pages[0] || null;
  const stat = data.stat || {};
  const duration = biliDuration(page && pages.length > 1 ? page.duration : data.duration);
  const meta = [
    duration && `时长 ${duration}`,
    biliDate(data.pubdate) && `发布于 ${biliDate(data.pubdate)}`,
    biliCount(stat.view) && `播放 ${biliCount(stat.view)}`,
    biliCount(stat.like) && `点赞 ${biliCount(stat.like)}`,
    pages.length > 1 && `共 ${pages.length} P，这是第 ${page ? page.page : 1} P${page && page.part ? `「${page.part}」` : ''}`,
  ].filter(Boolean).join(' · ');
  const desc = String(data.desc || '').replace(/\r\n/g, '\n').trim();
  return {
    ok: true, source: 'fuxi', url, kind: 'bilibili', site: 'B站',
    title: String(data.title).slice(0, 160),
    byline: String((data.owner && data.owner.name) || '').slice(0, 40),
    meta,
    description: desc === '-' ? '' : desc.slice(0, MAX_DESC),
    article: '',
    bvid: data.bvid || target.bvid || '',
    aid: data.aid || target.aid || '',
    cid: (page && page.cid) || data.cid,
    subtitle: '',
    subtitleLang: '',
  };
}

function pickSubtitle(list) {
  const subs = (Array.isArray(list) ? list : []).filter((s) => s && s.subtitle_url);
  const rank = (s) => {
    const lan = String(s.lan || '').toLowerCase();
    if (lan === 'zh-cn') return 0;
    if (lan === 'ai-zh') return 1;
    if (lan.startsWith('zh') || lan.startsWith('ai-zh')) return 2;
    return 3;
  };
  return subs.sort((a, b) => rank(a) - rank(b))[0] || null;
}

/** 字幕：拿不到就算了（不登录时常常是空的），任何错误都不影响主结果 */
async function readBiliSubtitle(info, opts) {
  try {
    const id = info.bvid ? `bvid=${encodeURIComponent(info.bvid)}` : `aid=${encodeURIComponent(info.aid)}`;
    const q = `${id}&cid=${encodeURIComponent(info.cid)}`;
    let player = null;
    for (const api of ['https://api.bilibili.com/x/player/wbi/v2', 'https://api.bilibili.com/x/player/v2']) {
      try {
        const json = await biliJson(`${api}?${q}`, opts);
        if (json && json.code === 0 && json.data) { player = json.data; break; }
      } catch {
        if (opts.signal && opts.signal.aborted) return;
        if (Date.now() >= opts.deadline) return;
      }
    }
    const sub = pickSubtitle(player && player.subtitle && player.subtitle.subtitles);
    if (!sub) return;
    let subUrl = String(sub.subtitle_url);
    if (subUrl.startsWith('//')) subUrl = 'https:' + subUrl;
    const u = new URL(subUrl);
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return;
    if (!BILI_SUB_HOSTS.test(u.hostname)) return; // 只去 B 站自己的 CDN 拿
    const json = await biliJson(u.toString(), opts);
    const text = (Array.isArray(json && json.body) ? json.body : [])
      .map((b) => String((b && b.content) || '').trim()).filter(Boolean).join('\n');
    if (!text) return;
    info.subtitle = text.slice(0, MAX_SUBTITLE);
    info.subtitleLength = text.length;
    info.subtitleLang = String(sub.lan_doc || sub.lan || '');
  } catch {
    /* 字幕失败不致命 */
  }
}

/** 读 B 站视频；任何主接口失败返回 null（调用方交回上游） */
async function readBilibili(target, url, opts) {
  try {
    if (target.short) {
      const resolved = await resolveBiliShort(url, opts);
      if (!resolved) return null;
      ({ target } = resolved);
    }
    const q = target.bvid ? `bvid=${encodeURIComponent(target.bvid)}` : `aid=${encodeURIComponent(target.aid)}`;
    const json = await biliJson(`https://api.bilibili.com/x/web-interface/view?${q}`, opts);
    if (!json || json.code !== 0) return null;
    const info = bilibiliInfo(json.data, target, url);
    if (!info) return null;
    await readBiliSubtitle(info, opts);
    return info;
  } catch {
    return null;
  }
}

function formatBilibili(info) {
  const head = [`链接标题：${info.title || '（无标题）'}`, `来源：${info.site || 'B站'}`];
  if (info.byline) head.push(`UP 主：${info.byline}`);
  if (info.meta) head.push(info.meta);
  const note = info.subtitle
    ? '（你读到的是视频简介和字幕文字，没有看到画面。）'
    : '（这个视频没拿到字幕，你只读到了标题和简介，没有看到画面，别猜视频内容。）';
  const desc = String(info.description || '').trim();
  const sub = String(info.subtitle || '').trim();
  const fixed = head.join('\n').length + note.length + 2;
  let room = Math.max(160, PROMPT_BUDGET - fixed);
  const parts = [...head];
  if (desc) {
    // 有字幕时简介最多占一半，剩下留给字幕
    const descRoom = sub ? Math.min(desc.length, Math.max(120, Math.floor(room / 2))) : room - 10;
    if (desc.length <= descRoom) parts.push(`简介：${desc}`);
    else parts.push(`简介（只是开头）：${desc.slice(0, descRoom)}…`);
    room -= Math.min(desc.length, descRoom) + 12;
  }
  if (sub) {
    const total = Math.max(sub.length, Number(info.subtitleLength) || 0);
    const label = `字幕${info.subtitleLang ? `（${info.subtitleLang}）` : ''}`;
    const subRoom = Math.max(60, room - 40);
    if (sub.length <= subRoom && total <= sub.length) parts.push(`${label}：`, sub);
    else parts.push(`${label}（约 ${total} 字，下面只是开头一段，不是全文）：`, sub.slice(0, subRoom) + '…');
  }
  parts.push(note);
  return parts.join('\n');
}

// ---------- 普通网页 ----------

let activeParsers = 0;

function parseInWorker(html, url, { timeoutMs, signal }) {
  if (signal && signal.aborted) return Promise.resolve({ error: 'aborted' });
  if (activeParsers >= MAX_PARSERS) return Promise.resolve({ error: 'parse_busy' });
  return new Promise((resolve) => {
    let worker;
    let timer;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      activeParsers -= 1;
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', abort);
      if (worker) worker.terminate().catch(() => {});
      resolve(value);
    };
    const abort = () => finish({ error: 'aborted' });
    activeParsers += 1;
    try {
      worker = new Worker(path.join(__dirname, 'linkWorker.js'), {
        workerData: { html, url }, resourceLimits: { maxOldGenerationSizeMb: 96 },
      });
      worker.once('message', finish);
      worker.once('error', () => finish({ error: 'parse_failed' }));
      worker.once('exit', () => finish({ error: 'parse_failed' }));
      timer = setTimeout(() => finish({ error: 'parse_timeout' }), Math.max(300, Math.min(3000, timeoutMs)));
      if (signal) signal.addEventListener('abort', abort, { once: true });
    } catch {
      finish({ error: 'parse_failed' });
    }
  });
}

/** 读普通网页；跳转到了短视频/笔记平台时返回 { handoff: 最终地址 }，交给 B 站读取或上游处理 */
async function readPage(url, opts, deadline) {
  const res = await links.fetchUrl(url, opts);
  if (!res.ok) return fail(url, res.error);
  if (res.status >= 400) return fail(url, `http_${res.status}`);
  const finalUrl = res.finalUrl || url;
  if (SHORT_KINDS.has(links.kindOf(finalUrl))) return { handoff: finalUrl };
  const type = String(res.contentType || '').toLowerCase();
  if (/^text\/(plain|markdown)/.test(type)) {
    const text = String(res.body || '').trim();
    if (!text) return fail(url, 'empty');
    return { ok: true, source: 'fuxi', url: finalUrl, kind: 'web', title: '', description: '', site: '', byline: '',
      article: text.slice(0, MAX_ARTICLE) };
  }
  if (type && !/html|xml/.test(type)) return fail(url, 'not_html');
  const info = await parseInWorker(res.body, finalUrl, { timeoutMs: deadline - Date.now(), signal: opts.signal });
  if (info.error) return fail(url, info.error);
  const ok = Boolean(info.title || info.description || info.article);
  if (!ok) return fail(url, 'empty');
  return {
    ok: true, source: 'fuxi', url: finalUrl, kind: links.kindOf(finalUrl),
    title: String(info.title || '').slice(0, 160),
    description: String(info.description || '').slice(0, 300),
    site: String(info.site || '').slice(0, 40),
    byline: String(info.byline || '').replace(/^\s*(作者|文|by)\s*[:：]?\s*/i, '').slice(0, 40),
    article: String(info.article || '').slice(0, MAX_ARTICLE),
    fullLength: String(info.article || '').length,
  };
}

// ---------- 对内核的接口 ----------

const cache = new Map();

/** 与上游 describeLink 同签名：返回 {ok, url, kind, title, description, article, error, …} */
async function describeLink(urlString, opts = {}) {
  const { timeoutMs = 6000, cacheMinutes = 60, allowPrivate = false, signal } = opts;
  const key = String(urlString || '').slice(0, 500);
  const bili = bilibiliTarget(key);
  if (!bili && SHORT_KINDS.has(links.kindOf(key))) return upstream.describeLink(urlString, opts);
  if (signal && signal.aborted) return fail(key, 'aborted');

  const cacheKey = key + (allowPrivate ? '|p' : '');
  const hit = cache.get(cacheKey);
  if (hit && Date.now() - hit.at < hit.ttl * 60 * 1000) return { ...hit.value, cached: true };

  const deadline = Date.now() + Math.max(1, Number(timeoutMs) || 6000);
  const fetchOpts = { timeoutMs, allowPrivate, deadline, signal };
  const handBack = () => upstream.describeLink(key, { ...opts, timeoutMs: Math.max(1000, deadline - Date.now()) });
  let value = null;
  if (bili) {
    value = await readBilibili(bili, key, fetchOpts);
    // API 不通（风控、视频失效、短链跳到非视频页……）：上游照旧读标题和文案
    if (!value) return signal && signal.aborted ? fail(key, 'aborted') : handBack();
  }
  const gh = !value && githubTarget(key);
  if (gh) value = await readGithub(gh, key, fetchOpts);
  if (!value) value = await readPage(key, fetchOpts, deadline);
  if (value && value.handoff) {
    // 其它短链跳到了 B 站视频：走 B 站 API；跳到其它视频/笔记平台：上游按视频处理（只读标题和文案）
    const target = bilibiliTarget(value.handoff);
    value = target && !target.short ? await readBilibili(target, value.handoff, fetchOpts) : null;
    if (!value) return handBack();
  }
  if (value.error === 'aborted' || value.error === 'parse_busy') return value;
  // 失败只短暂缓存：一次网络抖动不该让同一链接一小时内都读不了
  cache.set(cacheKey, { at: Date.now(), value, ttl: value.ok ? cacheMinutes : Math.min(2, cacheMinutes) });
  if (cache.size > 100) cache.delete(cache.keys().next().value);
  return value;
}

/** 与上游 formatForPrompt 同签名：压成一段给模型看的文字，总长控制在 linkBlock 的预算内 */
function formatForPrompt(info) {
  if (!info) return '';
  if (info.source !== 'fuxi') return upstream.formatForPrompt(info);
  if (!info.ok) return `对方发了一个链接（${info.url}），但没读到内容：${reasonText(info.error)}。`;
  if (info.kind === 'bilibili') return formatBilibili(info);

  const head = [`链接标题：${info.title || '（无标题）'}`];
  if (info.site) head.push(`来源：${info.site}`);
  if (info.byline) head.push(`作者：${info.byline}`);
  if (info.meta) head.push(info.meta);
  const article = String(info.article || '').trim();
  const desc = String(info.description || '').trim();
  const squash = (t) => t.replace(/\s+/g, '');
  const descKey = squash(desc).slice(0, 30);
  if (desc && !squash(article).startsWith(descKey) && !squash(info.title || '').includes(descKey)) {
    head.push(`简介：${desc.slice(0, 160)}`);
  }
  if (!article) return head.join('\n');

  const NOTE_ROOM = 40;
  const room = Math.max(100, PROMPT_BUDGET - head.join('\n').length - NOTE_ROOM - 4);
  if (article.length <= room) return [...head, '正文：', article].join('\n');
  const total = Math.max(article.length, Number(info.fullLength) || 0);
  const size = total > MAX_ARTICLE ? `超过 ${MAX_ARTICLE}` : `约 ${total}`;
  return [...head, `（正文${size} 字，下面只是开头一段，不是全文）`, '正文：',
    article.slice(0, room) + '…'].join('\n');
}

/** 替换上游导出；依赖没装（没跑 npm ci）就保持上游行为 */
function install(log = console) {
  try {
    require.resolve('linkedom');
    require.resolve('@mozilla/readability');
  } catch {
    log.warn('[fuxi] 链接阅读增强未启用：kernel/fuxi 依赖没装，沿用内核原逻辑');
    return false;
  }
  links.describeLink = describeLink;
  links.formatForPrompt = formatForPrompt;
  return true;
}

module.exports = {
  install, describeLink, formatForPrompt, githubTarget, githubInfo, bilibiliTarget, bilibiliInfo, reasonText, upstream,
};
