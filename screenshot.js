// screenshot.js
// Открывает battle2.html локально, ждёт готовности данных и шрифтов,
// компенсирует особенности рендеринга Linux (жирность/ClearType)
// и сохраняет идеальный скриншот контейнера.

const { chromium } = require("playwright-core");

const URL = process.env.SCREENSHOT_URL || "http://localhost:8080/battle2.html";
const OUT = process.env.SCREENSHOT_OUT || "battle_preview.png";
const VIEWPORT = { width: 1200, height: 900 };
const DEVICE_SCALE_FACTOR = 1.25; // Масштаб 125% под экран вашего ноутбука
const READY_TIMEOUT_MS = 20000;

async function main() {
  const browser = await chromium.launch({
    channel: "chrome",
    args: [
      "--force-color-profile=srgb",
      "--hide-scrollbars",
      "--enable-font-antialiasing",
    ],
  });

  try {
    const page = await browser.newPage({
      viewport: VIEWPORT,
      deviceScaleFactor: DEVICE_SCALE_FACTOR,
    });

    console.log(`Открываю ${URL}`);
    await page.goto(URL, { waitUntil: "networkidle" });

    // 1. Ждём сигнала от страницы: данные применились и всё отрисовалось
    await page.waitForFunction(() => window.RENDER_READY === true, null, {
      timeout: READY_TIMEOUT_MS,
    });

    // 2. Ждём реальной готовности шрифтов
    await page.evaluate(() => document.fonts && document.fonts.ready);
    const isFontLoaded = await page.evaluate(() => document.fonts.check('11px "CGCHR"'));
    console.log(`Шрифт CGCHR: ${isFontLoaded ? "загружен успешно" : "НЕ загружен (fallback)"}`);

    // 3. Замораживаем анимации, прячем панель автобоя,
    //    добавляем плотность шрифту (ClearType) и отодвигаем от рамки
    await page.addStyleTag({
      content: `
        *, *::before, *::after {
          animation-play-state: paused !important;
          animation-duration: 0s !important;
          transition: none !important;
        }
        .auto-bar { 
          display: none !important; 
        }

        /* Компенсация жирности шрифта на Linux и отступы от рамки */
        .avatar-name {
          -webkit-text-stroke: 0.35px currentColor !important;
          text-shadow: 0 0 0.5px currentColor !important;
          padding: 4px 4px 2px !important;
        }
      `,
    });

    // Пауза, чтобы стили гарантированно применились перед кадром
    await page.waitForTimeout(150);

    // 4. Снимаем именно блок .container (без лишней пустоты снизу)
    await page.locator(".container").screenshot({ path: OUT });
    console.log(`Скриншот успешно сохранён: ${OUT}`);
  } finally {
    await browser.close();
  }
}

main().catch(err => {
  console.error("Ошибка скриншота:", err);
  process.exit(1);
});
