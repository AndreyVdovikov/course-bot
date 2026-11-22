/**
 * Скрипт для Google Таблицы бота.
 * Вставить: Таблица → Расширения → Apps Script → заменить всё этим кодом.
 *
 * Делает две вещи:
 *  1) хранит учеников бота на листе "users" (бот читает и пишет сюда);
 *  2) раз в час будит бота на Render, чтобы он разослал уроки, кому пора.
 */

// ↓↓↓ ЗАПОЛНИ ↓↓↓
const SECRET  = 'ПРИДУМАЙ_ПАРОЛЬ';                  // тот же, что SECRET на Render (латиница, цифры, _ и -)
const BOT_URL = 'https://course-bot.onrender.com';  // адрес сервиса на Render
// ↑↑↑ ЗАПОЛНИ ↑↑↑

const SHEET = 'users';
const FIELDS = ['user_id', 'username', 'first_name', 'joined_at', 'started_at',
                'next_lesson', 'paused', 'offer_sent', 'blocked'];
const EXTRA = ['Пришёл', 'Начал курс', 'Уроков получено', 'Статус']; // для тебя, бот их не читает

function sheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(SHEET);
  if (!sh) sh = ss.insertSheet(SHEET);
  if (sh.getLastRow() === 0) {
    sh.appendRow(FIELDS.concat(EXTRA));
    sh.setFrozenRows(1);
    sh.getRange(1, 1, 1, FIELDS.length + EXTRA.length).setFontWeight('bold');
  }
  return sh;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  let req;
  try { req = JSON.parse(e.postData.contents); } catch (err) { return json_({ok: false, error: 'bad json'}); }
  if (req.key !== SECRET) return json_({ok: false, error: 'wrong key'});

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sh = sheet_();
    if (req.action === 'list') {
      const values = sh.getDataRange().getValues();
      const users = values.slice(1).map(r => {
        const o = {};
        FIELDS.forEach((f, i) => o[f] = r[i]);
        return o;
      });
      return json_({ok: true, users: users});
    }
    if (req.action === 'upsert') {
      const u = req.user;
      const row = FIELDS.map(f => (u[f] === null || u[f] === undefined) ? '' : u[f]);
      const toDate = t => t ? new Date(Number(t) * 1000) : '';
      const status = u.blocked ? 'заблокировал бота' : u.offer_sent ? 'получил оффер'
                   : u.paused ? 'отписался, пауза' : u.started_at ? 'проходит курс' : 'не подписался';
      const full = row.concat([toDate(u.joined_at), toDate(u.started_at), u.next_lesson || 0, status]);

      const ids = sh.getRange(1, 1, Math.max(sh.getLastRow(), 1), 1).getValues().map(r => String(r[0]));
      const idx = ids.indexOf(String(u.user_id));
      if (idx > 0) {
        sh.getRange(idx + 1, 1, 1, full.length).setValues([full]);
      } else {
        sh.appendRow(full);
      }
      return json_({ok: true});
    }
    return json_({ok: false, error: 'unknown action'});
  } finally {
    lock.releaseLock();
  }
}

/** Будит бота. Вызывается триггером раз в час. */
function tick() {
  UrlFetchApp.fetch(BOT_URL + '/tick?key=' + encodeURIComponent(SECRET), {muteHttpExceptions: true});
}

/** Запусти ОДИН раз вручную (кнопка ▶ Run): создаёт лист и ежечасный будильник. */
function setup() {
  sheet_();
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('tick').timeBased().everyHours(1).create();
}
