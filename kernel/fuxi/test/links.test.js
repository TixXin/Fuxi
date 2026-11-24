'use strict';
// 浮汐链接阅读增强的单元测试：node --test kernel/fuxi/test/
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fuxi = require('../links');
const links = require('../../src/adapters/links');

const para = (n) => Array.from({ length: n }, (_, i) =>
  `<p>这是第 ${i + 1} 段正文，讲的是一个足够长的话题，用来让 Readability 认出这里是文章主体而不是导航。</p>`).join('\n');

const PAGES = {
  '/article': ['text/html; charset=utf-8', `<!doctype html><html><head><title>测试文章 - 某站</title>
    <meta property="og:site_name" content="某站"><meta name="description" content="一句话简介"></head><body>
    <nav><a href="/">首页</a><a href="/a">产品</a><a href="/b">登录</a></nav>
    <article><h1>测试文章</h1>${para(8)}</article>
    <footer>版权所有</footer></body></html>`],
  '/long': ['text/html', `<html><head><title>长文</title></head><body><article>${para(80)}</article></body></html>`],
  '/plain': ['text/plain; charset=utf-8', '纯文本内容\n第二行'],
  '/image': ['image/png', 'PNGDATA'],
};

let server;
let base;
test.before(async () => {
  server = http.createServer((req, res) => {
    const page = PAGES[req.url];
    if (!page) { res.writeHead(404, { 'Content-Type': 'text/html' }); return res.end('<p>not found</p>'); }
    res.writeHead(200, { 'Content-Type': page[0] });
    res.end(page[1]);
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${server.address().port}`;
});
test.after(() => server.close());

const read = (p) => fuxi.describeLink(base + p, { allowPrivate: true, cacheMinutes: 0, timeoutMs: 5000 });

test('文章页：抽出正文并保留段落，导航和页脚不进来', async () => {
  const info = await read('/article');
  assert.equal(info.ok, true);
  assert.equal(info.site, '某站');
  assert.match(info.article, /第 1 段正文/);
  assert.match(info.article, /第 8 段正文/);
  assert.ok(info.article.split('\n').length >= 8, '段落之间应有换行');
  assert.doesNotMatch(info.article, /登录|版权所有/);
  const text = fuxi.formatForPrompt(info);
  assert.match(text, /^链接标题：/);
  assert.doesNotMatch(text, /不是全文/);
});

test('长文：提示词不超过内核 linkBlock 的 1200 字，并说明只是开头', async () => {
  const text = fuxi.formatForPrompt(await read('/long'));
  assert.ok(text.length <= 1200, `实际 ${text.length} 字`);
  assert.match(text, /下面只是开头一段，不是全文/);
});

test('失败情况给出中文原因', async () => {
  assert.match(fuxi.formatForPrompt(await read('/missing')), /页面不存在（404）/);
  assert.match(fuxi.formatForPrompt(await read('/image')), /不是网页/);
  const priv = await fuxi.describeLink(base + '/article', { cacheMinutes: 0 });
  assert.match(fuxi.formatForPrompt(priv), /内网或本机地址/);
});

test('纯文本直接当正文', async () => {
  const info = await read('/plain');
  assert.equal(info.ok, true);
  assert.match(info.article, /第二行/);
});

test('识别 GitHub issue / PR / release 链接', () => {
  assert.deepEqual(fuxi.githubTarget('https://github.com/a/b/issues/12#issuecomment-1'),
    { owner: 'a', repo: 'b', kind: 'issue', number: '12', api: 'https://api.github.com/repos/a/b/issues/12' });
  assert.equal(fuxi.githubTarget('https://github.com/a/b/pull/3/files').kind, 'pr');
  assert.equal(fuxi.githubTarget('https://github.com/a/b/releases/tag/v1.2.0').api,
    'https://api.github.com/repos/a/b/releases/tags/v1.2.0');
  assert.equal(fuxi.githubTarget('https://github.com/a/b'), null);
  assert.equal(fuxi.githubTarget('https://gitlab.com/a/b/issues/1'), null);
});

test('GitHub API 结果：PR 合并状态、issue 标签、正文去掉注释', () => {
  const t = fuxi.githubTarget('https://github.com/a/b/pull/7');
  const pr = fuxi.githubInfo(t, { number: 7, title: '修复', state: 'closed', comments: 2, body: '<!-- 模板 -->改了X',
    pull_request: { merged_at: '2026-11-20T01:00:00Z' }, user: { login: 'u' }, labels: [{ name: 'bug' }] });
  assert.equal(pr.title, 'a/b PR #7：修复');
  assert.match(pr.meta, /已合并（2026-11-20）/);
  assert.match(pr.meta, /标签 bug/);
  assert.equal(pr.article, '改了X');
  const issue = fuxi.githubInfo(fuxi.githubTarget('https://github.com/a/b/issues/8'),
    { number: 8, title: '崩溃', state: 'closed', state_reason: 'completed', body: '' });
  assert.match(issue.meta, /已解决关闭/);
  assert.equal(fuxi.githubInfo(t, { message: 'Not Found' }), null);
});

test('短视频链接交回上游逻辑；install 替换上游导出', () => {
  const before = links.describeLink;
  try {
    assert.equal(fuxi.install({ warn() {} }), true);
    assert.equal(links.describeLink, fuxi.describeLink);
    assert.equal(links.formatForPrompt, fuxi.formatForPrompt);
    // 上游结果（没有 source 标记）照旧用上游格式
    const up = { ok: true, url: 'https://b23.tv/x', kind: 'video', title: '视频', description: '文案' };
    assert.match(fuxi.formatForPrompt(up), /短视频平台/);
  } finally {
    links.describeLink = before;
    links.formatForPrompt = fuxi.upstream.formatForPrompt;
  }
});
