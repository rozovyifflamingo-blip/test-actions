// screenshot.js
// Открывает battle2.html локально (через уже поднятый http.server),
// дожидается window.RENDER_READY, замораживает анимации и снимает
// PNG всей видимой страницы — тот самый набор гарантий "точь-в-точь
// как в браузере", описанный в переписке: фиксированный viewport,
// готовые шрифты, применённые данные, никаких CSS-переходов в кадре.
//
// Использует playwright-core с системным Chrome (channel: "chrome"),
// чтобы не тянуть отдельный бандлированный Chromium — на раннерах
// GitHub Actions Chrome уже установлен.

const { chromium } = require("playwright-core");

const URL = process.env.SCREENSHOT_URL || "http://localhost:8080/battle2.html";
const OUT = process.env.SCREENSHOT_OUT || "battle_preview.png";
const VIEWPORT = { width: 1200, height: 900 };
const DEVICE_SCALE_FACTOR = 2;   // резкость для pixel-art шрифтов
const READY_TIMEOUT_MS = 20000;

async function main() {
  const browser = await chromium.launch({
    channel: "chrome",             // системный Google Chrome, не отдельный Chromium
    args: ["--force-color-profile=srgb", "--hide-scrollbars"],
  });

  try {
    const page = await browser.newPage({
      viewport: VIEWPORT,
      deviceScaleFactor: DEVICE_SCALE_FACTOR,
    });

    console.log(`Открываю ${URL}`);
    await page.goto(URL, { waitUntil: "networkidle" });

    // 1. Ждём, пока страница сама скажет "я применила данные и отрисовалась"
    await page.waitForFunction(() => window.RENDER_READY === true, null, {
      timeout: READY_TIMEOUT_MS,
    });

    // 2. Ждём готовности шрифтов (Press Start 2P, CGCHR — грузятся асинхронно)
    await page.evaluate(() => document.fonts && document.fonts.ready);

    // 3. Замораживаем анимации/переходы, чтобы не поймать HP-бар
    //    или эффект удара в промежуточном кадре
    await page.addStyleTag({
      content: `
        *, *::before, *::after {
          animation-play-state: paused !important;
          animation-duration: 0s !important;
          transition: none !important;
        }
      `,
    });

    // небольшая пауза, чтобы стиль точно применился перед снимком
    await page.waitForTimeout(150);

    await page.screenshot({ path: OUT, fullPage: true });
    console.log(`Скриншот сохранён: ${OUT}`);
  } finally {
    await browser.close();
  }
}

main().catch(err => {
  console.error("Ошибка скриншота:", err);
  process.exit(1);
});
