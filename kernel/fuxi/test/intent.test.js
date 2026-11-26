'use strict';
// 固定话术判断收紧的单元测试：node --test kernel/fuxi/test/
const test = require('node:test');
const assert = require('node:assert/strict');
const intent = require('../intent');

intent.install();
const reminder = require('../../src/core/reminder');
const canned = require('../../src/pipeline/stages/canned');

test('ownWords 去掉截图识别和被引用的内容，留下本人打的字', () => {
  assert.equal(intent.ownWords('[图片]（图片内容：截图文字(OCR)：走了 / 退下（快捷键））\n下个 beta 发不'), '下个 beta 发不');
  assert.equal(intent.ownWords('对\n（引用 Wyy 的消息：这个会话显示好像有问题）'), '对');
  assert.equal(intent.ownWords('[图片]（图片内容：一个设置页面）'), '');
  assert.equal(intent.ownWords('[语音 3秒]（语音内容：取消明天的提醒）'), '[语音 3秒]（语音内容：取消明天的提醒）');
});

test('群里的普通话不再被当成取消提醒（2026-11-26 用户群真实发言）', () => {
  for (const s of [
    '我放到 agent 上面之后就变宽了然后就移到别的标签了\n这个会突然变宽是什么原因',
    '这个全局快捷键可不可以设置成打开和关闭是同一个组合键 就跟电脑微信一样 打开页面和关闭页面是同一个组合键 不用再去按esc 没有快捷键的页面就用esc关',
    '反馈一下bug 最新版3.14不知道为啥一直弹这个密钥验证',
    '[图片]（图片内容：钥匙串访问 想要使用你的机密信息 取消 / 允许 / 这个）\n反馈一下bug',
    '别管那个了',
    '不要这个颜色',
  ]) {
    assert.equal(reminder.parseReminderCancellation(s), null, s);
  }
});

test('真正撤回提醒的说法照旧能认', () => {
  for (const s of ['取消提醒', '明天别提醒我买菜', '第三天的事情取消', '取消明天的提醒', '不用提醒我开会了', '把那件事取消吧']) {
    assert.ok(reminder.parseReminderCancellation(s), s);
  }
});

test('群里没叫她：不是主人、没找到提醒都不出声；真撤回了才说', () => {
  const group = { isGroup: true, atMe: false, msg: {} };
  assert.equal(intent.keepReminderReply({ reason: 'not_owner', canned: '群里的提醒统一由已绑定 ID 的主人取消' }, group), false);
  assert.equal(intent.keepReminderReply({ reason: 'reminder_cancel', canned: '没有找到对应的待发提醒。' }, group), false);
  assert.equal(intent.keepReminderReply({ reason: 'reminder_cancel', canned: '这个会话没有待发的提醒。' }, group), false);
  assert.equal(intent.keepReminderReply({ reason: 'reminder_cancel_ambiguous', canned: '我找到多条提醒' }, group), false);
  assert.equal(intent.keepReminderReply({ reason: 'reminder_cancel', canned: '已经取消 1 条提醒。' }, group), true);
  assert.equal(intent.keepReminderReply({ reason: 'reminder_cancel', canned: '取消了 2 条提醒。' }, group), true);
  assert.equal(intent.keepReminderReply({ reason: 'reminder_set', canned: '好，十点提醒你' }, group), true);
  assert.equal(intent.keepReminderReply({ reason: 'not_owner' }, { isGroup: true, atMe: true, msg: {} }), true);
  assert.equal(intent.keepReminderReply({ reason: 'not_owner' }, { isGroup: true, atMe: false, msg: { replyToBot: true } }), true);
  assert.equal(intent.keepReminderReply({ reason: 'not_owner' }, { isGroup: false, atMe: false, msg: {} }), true);
});

test('截图里的「走了」不会让她退场；主人亲口说「走吧」照旧退场', () => {
  const bot = { config: { persona: {}, group: { silentMinutes: 10 } }, rng: Math.random, logger: null };
  const base = { msg: { senderName: '煜哥' }, isGroup: true, isMaster: true, atMe: false, nowMs: Date.now(), key: 'k' };
  const shot = canned.dismissReply(bot, { ...base, state: {}, text: '[图片]（图片内容：截图文字(OCR)：走了）\n下个 beta 发不' });
  assert.equal(shot, null);
  const said = canned.dismissReply(bot, { ...base, state: {}, text: '走吧' });
  assert.ok(said && said.reason === 'dismissed');
});

test('chat.js 拿到的是包过的 canned 函数', () => {
  const { Bot } = require('../../src/pipeline/chat');
  assert.equal(typeof Bot.prototype.tryReminder, 'function');
  assert.equal(Bot.prototype.tryReminder.name, 'wrappedTryReminder');
  assert.equal(canned.dismissReply.name, 'wrapped');
  // chat.js 在 install() 里才第一次被加载，解构到的就是上面这个包装
  const chatPath = require.resolve('../../src/pipeline/chat');
  const cannedPath = require.resolve('../../src/pipeline/stages/canned');
  const chatMod = require.cache[chatPath];
  assert.ok(chatMod.children.some((c) => c.id === cannedPath));
});
