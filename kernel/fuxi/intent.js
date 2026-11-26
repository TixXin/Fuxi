'use strict';
// 浮汐新增（不属于上游）：收紧几条「不经模型、直接回固定话术」的判断，不改上游文件。
//
// 1. 取消提醒：上游 parseReminderCancellation 只要句子里有「别 / 不用 / 不要 / 取消」再加
//    「这个 / 那个 / 明天」就当成撤回提醒。群里「移到别的标签」「不用再去按 esc」「这个快捷键」
//    都会命中，不在主人名单里的人就收到「群里的提醒统一由已绑定 ID 的主人取消」。
//    这里改成：「别 / 不用 / 不要」必须和「提醒」一起出现；「取消 / 撤销 / 删除」不带「提醒」时，
//    只认短句里的「那件事 / 事情 / 明天 / 第三天」这类明确指向。
//    群里没 @ 她、也没回复她时，只有真撤回了提醒才出声，其余（不是主人、没找到、要选哪条）都不接。
// 2. 只看本人打的字：桥接把截图识别结果（「（图片内容：…）」，看图失败时是 OCR 原文）和被引用的消息
//    （「（引用 某人 的消息：…）」）拼在正文里交给内核。截图里的「走了」「取消」会触发退场
//    （主人发截图收到「好的主人，我走了」并静默 10 分钟）或提醒话术。
//    提醒、退场、固定问答三处判断前先把这两段去掉；提示词里照旧能看到全文。
//
// 必须在任何地方 require('src/pipeline/chat') 之前 install()：chat.js 在加载时解构
// require('./stages/canned') 的导出，晚了就换不掉。

const PAREN_BLOCK = /（(?:图片内容|引用 [^：]{0,40} 的消息)：/;

/** 去掉桥接拼进来的「（图片内容：…）」「（引用 X 的消息：…）」，括号按全角配对，只留本人打的字。 */
function ownWords(text) {
  let s = String(text || '');
  for (let guard = 0; guard < 20; guard += 1) {
    const m = PAREN_BLOCK.exec(s);
    if (!m) break;
    let depth = 0;
    let end = s.length;
    for (let i = m.index; i < s.length; i += 1) {
      if (s[i] === '（') depth += 1;
      else if (s[i] === '）') {
        depth -= 1;
        if (depth === 0) { end = i + 1; break; }
      }
    }
    s = s.slice(0, m.index) + s.slice(end);
  }
  return s
    .replace(/\[(?:图片|视频[^\]]*|动画表情|文件[^\]]*)\]/g, '')
    .replace(/\n{2,}/g, '\n')
    .trim();
}

const WEAK_VERB = /(?:不用|不要|别(?![的人处家样个]))/;
const STRONG_VERB = /(?:取消|撤销|删除)/;
const STRONG_TARGET = /(?:那件事|事情|今天|明天|后天|大后天|第[一二两三四五六七八九十\d]+天)/;

/** 这句话够不够格当「撤回提醒」交给上游解析。 */
function looksLikeCancellation(text) {
  const s = String(text || '').trim();
  if (!s) return false;
  if (s.includes('提醒')) return STRONG_VERB.test(s) || WEAK_VERB.test(s);
  return STRONG_VERB.test(s) && STRONG_TARGET.test(s) && s.length <= 30;
}

const QUIET_IN_GROUP = new Set(['not_owner', 'reminder_cancel', 'reminder_cancel_ambiguous']);

/** 群里没叫她时，撤回提醒只在真撤回了东西时出声。 */
function keepReminderReply(result, { isGroup, atMe, msg }) {
  if (!result || !isGroup || atMe || (msg && msg.replyToBot)) return true;
  if (!QUIET_IN_GROUP.has(result.reason)) return true;
  return result.reason === 'reminder_cancel' && /^(?:已经)?取消了? ?[1-9]/.test(String(result.canned || ''));
}

let installed = false;

function install() {
  if (installed) return;
  installed = true;

  const reminder = require('../src/core/reminder');
  const original = reminder.parseReminderCancellation;
  reminder.parseReminderCancellation = function parseReminderCancellation(text) {
    const own = ownWords(text);
    return looksLikeCancellation(own) ? original(own) : null;
  };

  const canned = require('../src/pipeline/stages/canned');
  for (const name of ['dismissReply', 'cannedReply']) {
    const fn = canned[name];
    canned[name] = function wrapped(bot, ctx) {
      const own = ownWords(ctx && ctx.text);
      if (!own) return null;
      const out = fn(bot, { ...ctx, text: own });
      if (out && bot && bot.logger) bot.logger.info(`[fuxi] 固定回复 reason=${out.reason || name}`);
      return out;
    };
  }

  const { Bot } = require('../src/pipeline/chat');
  const tryReminder = Bot.prototype.tryReminder;
  Bot.prototype.tryReminder = async function wrappedTryReminder(args) {
    const own = ownWords(args && args.clean && args.clean.text);
    if (!own) return null;
    const out = await tryReminder.call(this, { ...args, clean: { ...args.clean, text: own } });
    if (!out) return null;
    if (!keepReminderReply(out, args)) {
      if (this.logger) this.logger.info(`[fuxi] 群里没叫她，不接提醒话术 reason=${out.reason}`);
      return null;
    }
    if (this.logger) this.logger.info(`[fuxi] 固定回复 reason=${out.reason}`);
    return out;
  };
}

module.exports = { install, ownWords, looksLikeCancellation, keepReminderReply };
