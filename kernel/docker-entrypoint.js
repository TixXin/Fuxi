// 浮汐新增（不属于上游）：容器启动时把环境变量写进 data/config.json；随后 CMD 再 exec node server.js。
// 只改这里列出的几项，其余设置保持用户在 data/config.json 里的值。
'use strict';
const fs = require('fs');
const path = require('path');

const root = __dirname;
const dataDir = path.join(root, 'data');
const cfgPath = path.join(dataDir, 'config.json');
fs.mkdirSync(dataDir, { recursive: true });

let cfg;
try {
  cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
} catch {
  cfg = JSON.parse(fs.readFileSync(path.join(root, 'config', 'config.example.json'), 'utf8'));
  // 首次生成时的容器默认值
  cfg.env = { ...(cfg.env || {}), autoDetect: false, timezone: 'Asia/Shanghai' };
}

const env = process.env;
const set = (obj, keys, val) => {
  if (val === undefined || val === '') return;
  let o = obj;
  for (const k of keys.slice(0, -1)) o = o[k] = o[k] && typeof o[k] === 'object' ? o[k] : {};
  o[keys[keys.length - 1]] = val;
};

set(cfg, ['gateway', 'wechat', 'inboundToken'], env.FUXI_KERNEL_TOKEN);
set(cfg, ['gateway', 'wechat', 'enabled'], true);
set(cfg, ['server', 'host'], '127.0.0.1'); // 只在共享网络命名空间的回环上监听
set(cfg, ['server', 'port'], Number(env.FUXI_KERNEL_PORT || 8787));
if (env.FUXI_OWNER_WXIDS) {
  set(cfg, ['persona', 'master', 'wxids'], env.FUXI_OWNER_WXIDS.split(',').map((s) => s.trim()).filter(Boolean));
}
// 模型 key 走环境变量 DEEPSEEK_API_KEY（内核原生支持），不落盘

const tmp = cfgPath + '.tmp';
fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2), { mode: 0o600 });
fs.renameSync(tmp, cfgPath);

