// screenshot.js
const { chromium } = require("playwright-core");

const URL = process.env.SCREENSHOT_URL || "http://localhost:8080/battle2.html";
const OUT = process.env.SCREENSHOT_OUT || "battle_preview.png";
const VIEWPORT = { width: 1200, height: 900 };
const DEVICE_SCALE_FACTOR = 1.25; // Ровно масштаб вашего ноутбука (125%)
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

    // 1. Ждём, пока страница применит данные и выставит флаг готовности
    await page.waitForFunction(() => window.RENDER_READY === true, null, {
      timeout: READY_TIMEOUT_MS,
    });

    // 2. Ждём не просто готовности менеджера шрифтов, а реального применения CGCHR
    await page.evaluate(() => document.fonts && document.fonts.ready);
    const isFontLoaded = await page.evaluate(() => document.fonts.check('11px "CGCHR"'));
    console.log(`Статус шрифта CGCHR: ${isFontLoaded ? "загружен" : "НЕ загружен (fallback)"}`);

    // 3. Замораживаем анимации и прячем лишние элементы
    await page.addStyleTag({
      content: `
        *, *::before, *::after {
          animation-play-state: paused !important;
          animation-duration: 0s !important;
          transition: none !important;
        }
        .auto-bar { display: none !important; }
      `,
    });

    await page.waitForTimeout(150);

    // 4. Снимаем контейнер
    await page.locator(".container").screenshot({ path: OUT });
    console.log(`Скриншот сохранён: ${OUT}`);
  } finally {
    await browser.close();
  }
}

main().catch(err => {
  console.error("Ошибка скриншота:", err);
  process.exit(1);
});
