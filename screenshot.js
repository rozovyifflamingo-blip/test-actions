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

const { chromium } = require("playwright-core");

const VIEWPORT = { width: 1200, height: 900 };
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

async function shootPage(browser, url, out, { transparent = false } = {}) {
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
    await shootPage(browser, TARGET_MAIN_URL, TARGET_MAIN_OUT);

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
