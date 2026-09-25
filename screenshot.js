// screenshot.js
// Открывает battle2.html (и, если задано, calendar.html) локально (через
// уже поднятый http.server), дожидается window.RENDER_READY, замораживает
// анимации и снимает PNG всей видимой страницы — тот самый набор гарантий
// "точь-в-точь как в браузере", описанный в переписке: фиксированный
// viewport, готовые шрифты, применённые данные, никаких CSS-переходов
// в кадре.
//
// Использует playwright-core с системным Chrome (channel: "chrome"),
// чтобы не тянуть отдельный бандлированный Chromium — на раннерах
// GitHub Actions Chrome уже установлен.

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright-core");

const VIEWPORT = { width: 1536, height: 735 };   // = viewport реального браузера (1920px при масштабе 125%)
const DEVICE_SCALE_FACTOR = 2;   // резкость для pixel-art шрифтов
const READY_TIMEOUT_MS = 20000;

// Основной таргет (battle2.html) — как раньше, через SCREENSHOT_URL/OUT.
// Второй, опциональный таргет — календарь; если явно не задан через
// SCREENSHOT_URL_CALENDAR, выводим его сами из основного URL, заменив
// имя файла на calendar.html.
const TARGET_MAIN_URL = process.env.SCREENSHOT_URL || "http://localhost:8080/battle2.html";
const TARGET_MAIN_OUT = process.env.SCREENSHOT_OUT || "battle_preview.png";

const TARGET_CALENDAR_URL =
  process.env.SCREENSHOT_URL_CALENDAR ||
  TARGET_MAIN_URL.replace(/[^/]+$/, "calendar.html");
const TARGET_CALENDAR_OUT = process.env.SCREENSHOT_OUT_CALENDAR || "calendar_preview.png";
// если явно поставить SCREENSHOT_SKIP_CALENDAR=1 — второй снимок не делаем
const SKIP_CALENDAR = process.env.SCREENSHOT_SKIP_CALENDAR === "1";

// ── Журнал боя (третий снимок) ─────────────────────────────────────────
// Только текст, слева, на прозрачном фоне, шрифт CGCHR из репозитория.
// Файл кладём рядом с основным скриншотом (в ту же папку), если явно не
// задан через SCREENSHOT_OUT_LOG. SCREENSHOT_SKIP_LOG=1 — не снимать.
const TARGET_LOG_OUT =
  process.env.SCREENSHOT_OUT_LOG ||
  path.join(path.dirname(TARGET_MAIN_OUT), "battle_log.png");
const SKIP_LOG = process.env.SCREENSHOT_SKIP_LOG === "1";

const LOG_FONT_FILE = path.join(__dirname, "CGCHR-Regular.otf");
const LOG_FONT_SIZE = 16;          // px (при DEVICE_SCALE_FACTOR=2 на PNG это 32px)
const LOG_LINE_HEIGHT = 1.5;
const LOG_LINE_GAP = 4;           // px — дополнительный отступ между строками
const LOG_TOP_PAD = 3;            // px — прозрачный отступ над первой строкой
const LOG_MAX_WIDTH = 1200;        // px; длинные строки переносятся
const LOG_NEWEST_FIRST = true;     // true — новые записи сверху; false — сверху старые
// Служебные записи, которые не должны попадать в картинку
const LOG_SKIP = [/^Нажмите на аватар/];
// Цвета рассчитаны на светлый фон форума (сам PNG прозрачный)
const LOG_COLOR_TEXT = "#222222";    // основной текст — тёмный
const LOG_COLOR_DAMAGE = "#c62828";  // урон — красный

async function shootPage(browser, url, out, { transparent = false, collectLog = false } = {}) {
  const page = await browser.newPage({
    viewport: VIEWPORT,
    deviceScaleFactor: DEVICE_SCALE_FACTOR,
  });

  try {
    console.log(`Открываю ${url}`);
    await page.goto(url, { waitUntil: "networkidle" });

    // 1. Ждём, пока страница сама скажет "я применила данные и отрисовалась"
    await page.waitForFunction(() => window.RENDER_READY === true, null, {
      timeout: READY_TIMEOUT_MS,
    });

    // 2. Ждём готовности шрифтов (Press Start 2P, CGCHR — грузятся асинхронно)
    await page.evaluate(() => document.fonts && document.fonts.ready);

    // 3. Замораживаем анимации/переходы, чтобы не поймать HP-бар
    //    или эффект удара в промежуточном кадре; плюс прячем панель
    //    автобоя/скорости — она не нужна на превью
    await page.addStyleTag({
      content: `
        *, *::before, *::after {
          animation-play-state: paused !important;
          animation-duration: 0s !important;
          transition: none !important;
        }
        .auto-bar { display: none !important; }

        /* Только для скриншота (в живом браузере это не грузится):
           хром на раннере рендерит пиксельный шрифт ников тоньше и
           ровно впритык к верхнему краю блока. Сдвигаем текст на
           3px вниз, чтобы визуально совпадало с тем, что видно
           в обычном браузере. */
        .avatar-name {
          padding-top: 6px !important; /* было 3px, +3px вниз */
        }

        /* Только для скриншота battle2.html: убираем крупный заголовок
           "БОЙ С БОССОМ" и декоративные линии по бокам от него — само
           слово "тактический экран" остаётся, и над ним получается
           небольшой прозрачный отступ вместо заголовка. Живую страницу
           это не трогает: правило приходит только сюда, в скриншот-вкладку.
           На calendar.html классов .header-deco/.header-sub нет — там
           это правило ни на что не влияет. */
        .header-deco { display: none !important; }
        .header { padding-top: 4px !important; }
        .header-sub { margin-top: 0 !important; }
      `,
    });

    // небольшая пауза, чтобы стиль точно применился перед снимком.
    // battle2.html сам откладывает часть эффектов удара (появление
    // кубиков, применение урона, перерисовку карточки) на 180-240мс
    // через внутренние setTimeout — ждём с запасом дольше этого,
    // иначе кадр ловится посреди отложенной отрисовки.
    await page.waitForTimeout(700);

    // Снимаем не всю страницу (там пустое пространство внизу, размер
    // которого меняется в зависимости от числа участников), а именно
    // .container — обёртку всего контента. Playwright сам подгоняет
    // кадр под фактическую высоту элемента, так что "лишний" низ
    // отрезается автоматически, сколько бы участников ни было.
    // (У calendar.html тот же класс .container на обёртке — тот же
    // приём работает без изменений.) Для calendar.html дополнительно
    // просят прозрачный фон вокруг таблицы: omitBackground убирает
    // белую подложку Chromium там, где страница сама ничего не
    // закрасила (сама таблица красится своим фоном и не страдает).
    await page.locator(".container").screenshot({ path: out, omitBackground: transparent });
    console.log(`Скриншот сохранён: ${out}`);

    // Забираем журнал боя прямо из страницы (глобальный logEntries) —
    // пока она ещё открыта. Разбор HTML — через DOMParser (он «мёртвый»,
    // скрипты и картинки из текста записей не выполняются).
    if (collectLog) {
      return await page.evaluate(() => {
        const toText = html =>
          new DOMParser().parseFromString(String(html), "text/html").body.textContent;
        const entries = typeof logEntries !== "undefined" ? logEntries : [];
        return {
          lines: entries.map(e => toText(e.text)),   // в странице: новые записи первыми
        };
      });
    }
  } finally {
    await page.close();
  }
}

// ── Журнал боя → PNG ────────────────────────────────────────────────────
const escapeHtml = t =>
  t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// Убираем эмодзи и значки (без графики — только текст), схлопываем пробелы.
function cleanLogLine(t) {
  return t
    .replace(/[\p{Extended_Pictographic}\u2713\u25B8\uFE0F\u200D]/gu, "")
    .replace(/\s+/g, " ")
    .trim();
}

// Урон выделяем красным: «12 ур», «25 урона»
const withDamageHighlight = t =>
  escapeHtml(t).replace(/(\d+\s*ур[а-яё]*)/gi, '<span class="dmg">$1</span>');

async function shootLog(browser, log, out) {
  let lines = (log.lines || []).slice();
  if (!LOG_NEWEST_FIRST) lines.reverse();
  lines = lines
    .filter(l => !LOG_SKIP.some(re => re.test(String(l).trim())))
    .map(cleanLogLine)
    .filter(Boolean);

  if (!lines.length) {
    console.log("Журнал боя пуст — картинку журнала не делаю.");
    return;
  }

  // Шрифт вшиваем в страницу как data:-URL — так он гарантированно
  // загрузится (about:blank не может тянуть шрифт с другого origin).
  const fontB64 = fs.readFileSync(LOG_FONT_FILE).toString("base64");

  const html = `<!doctype html><html><head><meta charset="utf-8"><style>
    @font-face {
      font-family: 'CGCHR';
      src: url(data:font/otf;base64,${fontB64}) format('opentype');
    }
    html, body { margin: 0; padding: 0; background: transparent; }
    #log {
      display: inline-block;
      max-width: ${LOG_MAX_WIDTH}px;
      padding: ${LOG_TOP_PAD}px 6px 4px;
      text-align: left;
      font-family: 'CGCHR', monospace;
      font-size: ${LOG_FONT_SIZE}px;
      line-height: ${LOG_LINE_HEIGHT};
      color: ${LOG_COLOR_TEXT};
    }
    .line { overflow-wrap: anywhere; }
    .line + .line { margin-top: ${LOG_LINE_GAP}px; }
    .dmg  { color: ${LOG_COLOR_DAMAGE}; }
  </style></head><body><div id="log">${
    lines.map(l => `<div class="line">${withDamageHighlight(l)}</div>`).join("")
  }</div></body></html>`;

  const page = await browser.newPage({
    viewport: VIEWPORT,
    deviceScaleFactor: DEVICE_SCALE_FACTOR,
  });
  try {
    await page.setContent(html, { waitUntil: "load" });
    await page.evaluate(async size => {
      await document.fonts.load(`${size}px CGCHR`);
      await document.fonts.ready;
    }, LOG_FONT_SIZE);
    // Глифы CGCHR выше строки (line-height) и без запаса упёрлись бы в верхний
    // край PNG. Меряем, насколько текст «вылезает» вверх, и добавляем это к
    // отступу — тогда над самыми высокими буквами первой строки остаётся
    // ровно LOG_TOP_PAD прозрачных пикселей.
    await page.evaluate(top => {
      const log = document.getElementById("log");
      const first = log.querySelector(".line");
      const range = document.createRange();
      range.selectNodeContents(first);
      const overflow = Math.max(
        0,
        first.getBoundingClientRect().top - range.getBoundingClientRect().top
      );
      log.style.paddingTop = (top + overflow) + "px";
    }, LOG_TOP_PAD);
    await page.locator("#log").screenshot({ path: out, omitBackground: true });
    console.log(`Журнал боя сохранён: ${out} (${lines.length} строк)`);
  } finally {
    await page.close();
  }
}

async function main() {
  const browser = await chromium.launch({
    channel: "chrome",             // системный Google Chrome, не отдельный Chromium
    args: ["--force-color-profile=srgb", "--hide-scrollbars"],
  });

  try {
    const log = await shootPage(browser, TARGET_MAIN_URL, TARGET_MAIN_OUT, { collectLog: !SKIP_LOG });

    if (!SKIP_LOG && log) {
      try {
        await shootLog(browser, log, TARGET_LOG_OUT);
      } catch (e) {
        // Журнал — дополнение: его сбой не должен валить основной скрин
        console.error("Не удалось снять журнал боя:", e.message || e);
      }
    }

    if (!SKIP_CALENDAR) {
      try {
        await shootPage(browser, TARGET_CALENDAR_URL, TARGET_CALENDAR_OUT, { transparent: true });
      } catch (e) {
        // Календарь — дополнение, а не критичная часть; если его снять
        // не удалось (например файла пока нет на раннере), не валим
        // весь шаг ради уже готового battle_preview.png.
        console.error(`Не удалось снять календарь (${TARGET_CALENDAR_URL}):`, e.message || e);
      }
    }
  } finally {
    await browser.close();
  }
}

main().catch(err => {
  console.error("Ошибка скриншота:", err);
  process.exit(1);
});
