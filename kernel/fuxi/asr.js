// 浮汐新增（不属于上游）：POST /api/fuxi/asr {audio: <WAV base64>} → {ok, text}
// 桥接把微信语音（SILK）解成 WAV 后交到这里转文字。服务商、地址、Key 全用内核的 voice 配置
// （默认跟随聊天模型：DeepSeek /v1/tools/asr 或千问 qwen3-asr-flash），调用方式与上游
// services/apiProbe.js 的「测试语音」一致，只是返回完整文本。
'use strict';
const { resolveVoiceConfig } = require('../src/infra/config');
const { voiceKey } = require('../src/services/apiProbe');
const { json, readBody } = require('../src/http/util');

const MAX_WAV = 4 * 1024 * 1024;

async function transcribe(config, wav, fetchImpl = fetch) {
  if (config.voice?.enabled === false) throw Object.assign(new Error('语音识别已在内核关闭'), { statusCode: 409 });
  const v = resolveVoiceConfig(config);
  if (v.supported === false) throw Object.assign(new Error(v.hint || '当前服务商不支持语音识别'), { statusCode: 409 });
  const url = new URL(v.baseUrl);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('语音接口地址无效');
  const key = v.auto ? v.apiKey : voiceKey(config, url.href);
  if (!key) throw Object.assign(new Error('没有可用的语音识别 Key'), { statusCode: 409 });
  const mode = v.provider || 'auto';
  const multipart = mode === 'deepseek' || (mode === 'auto' && (url.hostname === 'api.deepseek.com'
    || url.hostname.endsWith('.deepseek.com') || url.pathname.replace(/\/$/, '').endsWith('/v1/tools/asr')));
  const headers = { Authorization: 'Bearer ' + key };
  let body;
  if (multipart) {
    body = new FormData();
    body.append('file', new Blob([wav], { type: 'audio/wav' }), 'voice.wav');
  } else {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify({ model: v.model, input: { messages: [{ role: 'user', content: [{ audio: 'data:audio/wav;base64,' + wav.toString('base64') }] }] } });
  }
  const timeout = Number(v.timeoutMs) || 90000;
  const res = await fetchImpl(url.href, { method: 'POST', headers, body, redirect: 'error', signal: AbortSignal.timeout(timeout) });
  if (!res.ok) throw new Error('语音识别接口 HTTP ' + res.status);
  const data = await res.json();
  const text = multipart ? data?.text : data?.output?.choices?.[0]?.message?.content?.[0]?.text;
  return typeof text === 'string' ? text.trim() : '';
}

module.exports = function register(router) {
  router.post('/api/fuxi/asr', async (runtime, req, res) => {
    try {
      const body = await readBody(req, Math.ceil(MAX_WAV * 1.4));
      const wav = Buffer.from(String((body && body.audio) || ''), 'base64');
      if (!wav.length || wav.length > MAX_WAV || wav.toString('ascii', 0, 4) !== 'RIFF' || wav.toString('ascii', 8, 12) !== 'WAVE') {
        return json(res, 400, { ok: false, error: '需要不超过 4 MiB 的 WAV' });
      }
      const text = await transcribe(runtime.config, wav);
      return json(res, 200, { ok: true, text });
    } catch (err) {
      runtime.logger.warn('语音识别失败：' + (err && err.message));
      return json(res, err.statusCode || 502, { ok: false, error: String((err && err.message) || err) });
    }
  });
};
module.exports.transcribe = transcribe;
