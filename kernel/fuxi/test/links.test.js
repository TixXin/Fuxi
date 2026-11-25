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

test('识别 GitHub 仓库 / 文件 / issue / PR / release 链接', () => {
  assert.deepEqual(fuxi.githubTarget('https://github.com/a/b/issues/12#issuecomment-1'),
    { owner: 'a', repo: 'b', kind: 'issue', number: '12', api: 'https://api.github.com/repos/a/b/issues/12' });
  assert.equal(fuxi.githubTarget('https://github.com/a/b/pull/3/files').kind, 'pr');
  assert.equal(fuxi.githubTarget('https://github.com/a/b/releases/tag/v1.2.0').api,
    'https://api.github.com/repos/a/b/releases/tags/v1.2.0');
  assert.deepEqual(fuxi.githubTarget('https://github.com/a/b'),
    { owner: 'a', repo: 'b', kind: 'repo', api: 'https://api.github.com/repos/a/b', readmeApi: 'https://api.github.com/repos/a/b/readme' });
  assert.deepEqual(fuxi.githubTarget('https://github.com/a/b/blob/main/docs/README.md'),
    { owner: 'a', repo: 'b', kind: 'blob', ref: 'main', filePath: 'docs/README.md',
      api: 'https://api.github.com/repos/a/b/contents/docs/README.md?ref=main' });
  assert.equal(fuxi.githubTarget('https://github.com/a'), null);
  assert.equal(fuxi.githubTarget('https://gitlab.com/a/b/issues/1'), null);
});

test('GitHub API 结果：仓库 README、blob 文件、PR 合并状态、issue 标签', () => {
  const rt = fuxi.githubTarget('https://github.com/a/b');
  const repo = fuxi.githubInfo(rt, { full_name: 'a/b', description: '桌面小岛', language: 'Rust', stargazers_count: 99,
    pushed_at: '2026-11-24T00:00:00Z', license: { spdx_id: 'AGPL-3.0' } }, 'https://github.com/a/b', '# 标题\n正文内容');
  assert.equal(repo.title, 'a/b（GitHub 仓库）');
  assert.match(repo.meta, /语言 Rust/);
  assert.match(repo.meta, /Star 99/);
  assert.match(repo.article, /正文内容/);

  const bt = fuxi.githubTarget('https://github.com/a/b/blob/main/README.md');
  const blob = fuxi.githubInfo(bt, { type: 'file', path: 'README.md', size: 2048,
    content: Buffer.from('# 说明\n这是文件内容', 'utf8').toString('base64') }, 'https://github.com/a/b/blob/main/README.md');
  assert.equal(blob.title, 'a/b 文件：README.md');
  assert.match(blob.article, /这是文件内容/);

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

// ---------- B 站（全部用假 fetch，不走真实网络） ----------

const BV = 'BV1xx411c7mD';
const VIEW = {
  code: 0,
  data: {
    bvid: BV, aid: 170001, cid: 111, title: '测试视频标题', desc: '这是完整的视频简介\n第二行简介',
    duration: 3725, pubdate: 1763600000, owner: { name: '某UP主' }, stat: { view: 123456, like: 7890 },
    pages: [{ cid: 111, page: 1, part: '上', duration: 1800 }, { cid: 222, page: 2, part: '下', duration: 1925 }],
  },
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

/** 按 URL 前缀返回假响应，记下请求 */
function mockFetch(t, routes) {
  const calls = [];
  const real = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    for (const [prefix, handler] of routes) {
      if (String(url).startsWith(prefix)) return handler(String(url), init);
    }
    throw new Error('unexpected fetch ' + url);
  };
  t.after(() => { globalThis.fetch = real; });
  return calls;
}

/** 上游 describeLink 换成假的，记下是否被调用 */
function mockUpstream(t) {
  const real = fuxi.upstream.describeLink;
  const seen = [];
  fuxi.upstream.describeLink = async (url) => { seen.push(url); return { ok: true, url, kind: 'video', title: '上游', description: '' }; };
  t.after(() => { fuxi.upstream.describeLink = real; });
  return seen;
}

const readBili = (url) => fuxi.describeLink(url, { cacheMinutes: 0, timeoutMs: 3000 });

test('识别各种 B 站视频链接', () => {
  assert.deepEqual(fuxi.bilibiliTarget(`https://www.bilibili.com/video/${BV}/?spm_id_from=333`), { bvid: BV, page: 1 });
  assert.deepEqual(fuxi.bilibiliTarget(`https://m.bilibili.com/video/${BV}?p=2`), { bvid: BV, page: 2 });
  assert.deepEqual(fuxi.bilibiliTarget(`https://bilibili.com/video/${BV}`), { bvid: BV, page: 1 });
  assert.deepEqual(fuxi.bilibiliTarget('http://www.bilibili.com/video/av170001'), { aid: '170001', page: 1 });
  assert.deepEqual(fuxi.bilibiliTarget(`https://www.bilibili.com/video/bv${BV.slice(2)}`), { bvid: BV, page: 1 });
  assert.deepEqual(fuxi.bilibiliTarget('https://b23.tv/abc123'), { short: true });
  assert.equal(fuxi.bilibiliTarget('https://live.bilibili.com/123'), null);
  assert.equal(fuxi.bilibiliTarget('https://www.bilibili.com/read/cv123'), null);
  assert.equal(fuxi.bilibiliTarget('https://www.bilibili.com/video/BVshort'), null);
  assert.equal(fuxi.bilibiliTarget('https://evilbilibili.com/video/' + BV), null);
});

test('B 站：view + 字幕都拿到，带 UA 和 Referer，按中文输出', async (t) => {
  const upSeen = mockUpstream(t);
  const calls = mockFetch(t, [
    ['https://api.bilibili.com/x/web-interface/view', () => json(VIEW)],
    ['https://api.bilibili.com/x/player/wbi/v2', () => json({ code: 0, data: { subtitle: { subtitles: [
      { lan: 'en-US', lan_doc: 'English', subtitle_url: '//aisubtitle.hdslb.com/en.json' },
      { lan: 'ai-zh', lan_doc: '中文（自动生成）', subtitle_url: '//aisubtitle.hdslb.com/zh.json' },
    ] } } })],
    ['https://aisubtitle.hdslb.com/zh.json', () => json({ body: [{ content: '大家好' }, { content: '今天讲测试' }] })],
  ]);
  const info = await readBili(`https://www.bilibili.com/video/${BV}?p=2`);
  assert.equal(upSeen.length, 0);
  assert.equal(info.ok, true);
  assert.equal(info.source, 'fuxi');
  assert.equal(info.kind, 'bilibili');
  assert.equal(info.byline, '某UP主');
  assert.equal(info.cid, 222);
  assert.equal(info.subtitle, '大家好\n今天讲测试');
  assert.match(calls[0].url, new RegExp(`bvid=${BV}$`));
  assert.match(calls[1].url, /cid=222/);
  assert.equal(calls[0].init.headers.Referer, 'https://www.bilibili.com/');
  assert.match(calls[0].init.headers['User-Agent'], /Mozilla\/5\.0/);
  const text = fuxi.formatForPrompt(info);
  assert.match(text, /^链接标题：测试视频标题/);
  assert.match(text, /UP 主：某UP主/);
  assert.match(text, /时长 32:05/);
  assert.match(text, /播放 12\.3 万/);
  assert.match(text, /共 2 P，这是第 2 P「下」/);
  assert.match(text, /简介：这是完整的视频简介\n第二行简介/);
  assert.match(text, /字幕（中文（自动生成））：\n大家好\n今天讲测试/);
  assert.match(text, /没有看到画面/);
});

test('B 站：没有字幕时只给简介；长简介截断并说明只是开头，总长不超 1200', async (t) => {
  mockUpstream(t);
  const longDesc = '很长的简介。'.repeat(400);
  mockFetch(t, [
    ['https://api.bilibili.com/x/web-interface/view', () => json({ code: 0, data: { ...VIEW.data, desc: longDesc, pages: [] } })],
    ['https://api.bilibili.com/x/player/', () => json({ code: 0, data: { subtitle: { subtitles: [] } } })],
  ]);
  const info = await readBili(`https://www.bilibili.com/video/${BV}`);
  assert.equal(info.ok, true);
  assert.equal(info.subtitle, '');
  const text = fuxi.formatForPrompt(info);
  assert.ok(text.length <= 1200, `实际 ${text.length} 字`);
  assert.match(text, /简介（只是开头）/);
  assert.match(text, /没拿到字幕/);
  assert.doesNotMatch(text, /字幕（/);
});

test('B 站：字幕接口出错不影响主结果；长字幕截断', async (t) => {
  mockUpstream(t);
  mockFetch(t, [
    ['https://api.bilibili.com/x/web-interface/view', () => json(VIEW)],
    ['https://api.bilibili.com/x/player/wbi/v2', () => json({ code: -403, message: '访问权限不足' })],
    ['https://api.bilibili.com/x/player/v2', () => json({ code: 0, data: { subtitle: { subtitles: [
      { lan: 'zh-CN', lan_doc: '中文', subtitle_url: 'https://aisubtitle.hdslb.com/long.json' }] } } })],
    ['https://aisubtitle.hdslb.com/long.json', () => json({ body: Array.from({ length: 300 }, (_, i) => ({ content: `第${i}句字幕内容` })) })],
  ]);
  const info = await readBili(`https://www.bilibili.com/video/${BV}`);
  assert.ok(info.subtitle.length > 1000);
  const text = fuxi.formatForPrompt(info);
  assert.ok(text.length <= 1200, `实际 ${text.length} 字`);
  assert.match(text, /下面只是开头一段，不是全文/);

  // 字幕 JSON 本身挂了：照样返回视频信息
  globalThis.fetch = async (url) => {
    if (String(url).includes('/x/web-interface/view')) return json(VIEW);
    if (String(url).includes('/x/player/')) return json({ code: 0, data: { subtitle: { subtitles: [
      { lan: 'zh-CN', subtitle_url: '//aisubtitle.hdslb.com/x.json' }] } } });
    throw new Error('boom');
  };
  const info2 = await readBili(`https://www.bilibili.com/video/${BV}`);
  assert.equal(info2.ok, true);
  assert.equal(info2.subtitle, '');
});

test('B 站：字幕地址不是 B 站 CDN 就不去拿', async (t) => {
  mockUpstream(t);
  const calls = mockFetch(t, [
    ['https://api.bilibili.com/x/web-interface/view', () => json(VIEW)],
    ['https://api.bilibili.com/x/player/wbi/v2', () => json({ code: 0, data: { subtitle: { subtitles: [
      { lan: 'zh-CN', subtitle_url: 'https://evil.example/x.json' }] } } })],
  ]);
  const info = await readBili(`https://www.bilibili.com/video/${BV}`);
  assert.equal(info.subtitle, '');
  assert.ok(!calls.some((c) => c.url.includes('evil.example')));
});

test('B 站：b23.tv 短链先解析跳转再读 API', async (t) => {
  mockUpstream(t);
  const calls = mockFetch(t, [
    ['https://b23.tv/abc123', (_u, init) => {
      assert.equal(init.redirect, 'manual');
      return new Response(null, { status: 302, headers: { Location: `https://m.bilibili.com/video/${BV}?share_source=copy` } });
    }],
    ['https://api.bilibili.com/x/web-interface/view', () => json(VIEW)],
    ['https://api.bilibili.com/x/player/', () => json({ code: 0, data: { subtitle: { subtitles: [] } } })],
  ]);
  const info = await readBili('https://b23.tv/abc123');
  assert.equal(info.ok, true);
  assert.equal(info.title, '测试视频标题');
  assert.equal(info.url, 'https://b23.tv/abc123');
  assert.match(calls[1].url, new RegExp(`bvid=${BV}`));
});

test('B 站：API 出错、风控或短链跳到非视频页时交回上游', async (t) => {
  const upSeen = mockUpstream(t);
  mockFetch(t, [
    ['https://api.bilibili.com/x/web-interface/view?bvid=BV1xx411c7mD', () => json({ code: -404, message: '啥都木有' })],
    ['https://api.bilibili.com/x/web-interface/view?aid=1', () => json({}, 412)],
    ['https://api.bilibili.com/x/web-interface/view?aid=2', () => { throw new Error('ECONNRESET'); }],
    ['https://b23.tv/live', () => new Response(null, { status: 302, headers: { Location: 'https://live.bilibili.com/1' } })],
  ]);
  for (const url of [`https://www.bilibili.com/video/${BV}`, 'https://www.bilibili.com/video/av1',
    'https://www.bilibili.com/video/av2', 'https://b23.tv/live']) {
    const info = await readBili(url);
    assert.equal(info.title, '上游', url);
    assert.notEqual(info.source, 'fuxi');
  }
  assert.equal(upSeen.length, 4);
});

test('抖音、小红书照旧交回上游，不走 B 站 API', async (t) => {
  const upSeen = mockUpstream(t);
  const calls = mockFetch(t, []);
  await readBili('https://v.douyin.com/abc/');
  await readBili('https://www.xiaohongshu.com/explore/123');
  assert.equal(upSeen.length, 2);
  assert.equal(calls.length, 0);
});
