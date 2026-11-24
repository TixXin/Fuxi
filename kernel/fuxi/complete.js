// 浮汐新增（不属于上游）：POST /api/fuxi/complete {system, text, maxTokens?} → {ok, text}
// 给桥接做后台整理（群反馈分类、摘要）用：服务商、模型、Key 全部沿用内核的聊天配置，桥接不用另存 Key。
// 关闭思考、非流式，单次最长 120 秒。
'use strict';
const { json, readBody } = require('../src/http/util');

const MAX_TEXT = 60000;

async function complete(runtime, { system, text, maxTokens }) {
  const llm = runtime.llm;
  if (!llm || typeof llm.complete !== 'function') throw Object.assign(new Error('内核没有可用的模型'), { statusCode: 409 });
  if (llm.isMock) throw Object.assign(new Error('内核处于模拟模式，没有真实模型'), { statusCode: 409 });
  return llm.complete({
    system: String(system || '').slice(0, 8000),
    turns: [{ role: 'user', content: String(text).slice(0, MAX_TEXT) }],
    maxTokens: Math.max(100, Math.min(4000, Number(maxTokens) || 1500)),
    thinking: false,
    purpose: 'background',
    signal: AbortSignal.timeout(120000),
  });
}

module.exports = function register(router) {
  router.post('/api/fuxi/complete', async (runtime, req, res) => {
    try {
      const body = await readBody(req, MAX_TEXT * 4 + 40000);
      if (!body || !String(body.text || '').trim()) return json(res, 400, { ok: false, error: '缺少 text' });
      const text = await complete(runtime, body);
      return json(res, 200, { ok: true, text: String(text || '').trim() });
    } catch (err) {
      runtime.logger.warn('后台整理调用模型失败：' + (err && err.message));
      return json(res, err.statusCode || 502, { ok: false, error: String((err && err.message) || err) });
    }
  });
};
module.exports.complete = complete;
