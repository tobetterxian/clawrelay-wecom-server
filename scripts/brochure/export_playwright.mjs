import fs from "fs";
import path from "path";
import { pathToFileURL } from "url";
import { chromium } from "playwright";

const [, , mode, inputPath, outputPath] = process.argv;

if (!mode || !inputPath || !outputPath) {
  console.error("Usage: node export_playwright.mjs <pdf|image> <inputHtml> <outputFile>");
  process.exit(1);
}

const absoluteInput = path.resolve(inputPath);
const absoluteOutput = path.resolve(outputPath);

if (!fs.existsSync(absoluteInput)) {
  console.error(`Input file not found: ${absoluteInput}`);
  process.exit(1);
}

fs.mkdirSync(path.dirname(absoluteOutput), { recursive: true });

const browser = await chromium.launch({ headless: true });

try {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 2048 },
    deviceScaleFactor: 2,
  });

  await page.goto(pathToFileURL(absoluteInput).href, { waitUntil: "load" });
  await page.waitForTimeout(500);
  await page.emulateMedia({ media: "screen" });

  if (mode === "pdf") {
    await page.pdf({
      path: absoluteOutput,
      printBackground: true,
      preferCSSPageSize: true,
      format: "A4",
      margin: {
        top: "10mm",
        right: "10mm",
        bottom: "10mm",
        left: "10mm",
      },
    });
    console.log(`Exported PDF: ${absoluteOutput}`);
    process.exit(0);
  }

  if (mode === "image") {
    const pageSize = await page.evaluate(() => {
      const body = document.body;
      const doc = document.documentElement;
      const width = Math.max(
        body?.scrollWidth || 0,
        body?.offsetWidth || 0,
        doc?.clientWidth || 0,
        doc?.scrollWidth || 0,
        doc?.offsetWidth || 0,
        1280,
      );
      const height = Math.max(
        body?.scrollHeight || 0,
        body?.offsetHeight || 0,
        doc?.clientHeight || 0,
        doc?.scrollHeight || 0,
        doc?.offsetHeight || 0,
        720,
      );
      return {
        width: Math.min(width, 2000),
        height: Math.min(height, 16000),
      };
    });
    await page.setViewportSize(pageSize);
    await page.screenshot({
      path: absoluteOutput,
      fullPage: true,
      type: "png",
    });
    console.log(`Exported image: ${absoluteOutput}`);
    process.exit(0);
  }

  console.error(`Unsupported mode: ${mode}`);
  process.exit(1);
} finally {
  await browser.close();
}
