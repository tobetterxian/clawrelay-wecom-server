import fs from "fs";
import path from "path";
import PptxGenJS from "pptxgenjs";

const [, , outlinePath, outputPath] = process.argv;

if (!outlinePath || !outputPath) {
  console.error("Usage: node export_ppt.mjs <outlineMarkdown> <outputPptx>");
  process.exit(1);
}

const absoluteOutline = path.resolve(outlinePath);
const absoluteOutput = path.resolve(outputPath);

if (!fs.existsSync(absoluteOutline)) {
  console.error(`Outline file not found: ${absoluteOutline}`);
  process.exit(1);
}

fs.mkdirSync(path.dirname(absoluteOutput), { recursive: true });

function parseOutline(markdown) {
  const lines = markdown.replace(/\r/g, "").split("\n");
  let deckTitle = "产品画册";
  let currentSlide = null;
  const slides = [];

  const ensureSlide = () => {
    if (!currentSlide) {
      currentSlide = {
        title: deckTitle,
        bullets: [],
        paragraphs: [],
      };
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      continue;
    }

    if (line.startsWith("# ")) {
      deckTitle = line.slice(2).trim() || deckTitle;
      continue;
    }

    if (line.startsWith("## ")) {
      if (currentSlide) {
        slides.push(currentSlide);
      }
      currentSlide = {
        title: line.slice(3).trim() || `第 ${slides.length + 1} 页`,
        bullets: [],
        paragraphs: [],
      };
      continue;
    }

    ensureSlide();

    if (line.startsWith("### ")) {
      currentSlide.bullets.push(line.slice(4).trim());
      continue;
    }

    if (/^[-*+]\s+/.test(line)) {
      currentSlide.bullets.push(line.replace(/^[-*+]\s+/, "").trim());
      continue;
    }

    if (/^\d+[.)]\s+/.test(line)) {
      currentSlide.bullets.push(line.replace(/^\d+[.)]\s+/, "").trim());
      continue;
    }

    currentSlide.paragraphs.push(line);
  }

  if (currentSlide) {
    slides.push(currentSlide);
  }

  if (slides.length === 0) {
    slides.push({
      title: deckTitle,
      bullets: [],
      paragraphs: ["请先补充 `docs/brochure-outline.md` 的内容，再重新导出。"],
    });
  }

  return { deckTitle, slides };
}

const outlineContent = fs.readFileSync(absoluteOutline, "utf-8");
const { deckTitle, slides } = parseOutline(outlineContent);

const pptx = new PptxGenJS();
pptx.layout = "LAYOUT_WIDE";
pptx.author = "ClawRelay Codex";
pptx.company = "ClawRelay";
pptx.subject = deckTitle;
pptx.title = deckTitle;
pptx.lang = "zh-CN";

const cover = pptx.addSlide();
cover.background = { color: "F8FAFC" };
cover.addText(deckTitle, {
  x: 0.7,
  y: 1.1,
  w: 11.5,
  h: 0.8,
  fontSize: 24,
  bold: true,
  color: "0F172A",
});
cover.addText("自动生成的产品画册演示稿", {
  x: 0.7,
  y: 2.0,
  w: 7.0,
  h: 0.5,
  fontSize: 14,
  color: "475569",
});
cover.addText(path.basename(absoluteOutline), {
  x: 0.7,
  y: 2.5,
  w: 8.5,
  h: 0.4,
  fontSize: 10,
  color: "64748B",
});

slides.forEach((slideData, index) => {
  const slide = pptx.addSlide();
  slide.background = { color: "FFFFFF" };

  slide.addText(slideData.title || `第 ${index + 1} 页`, {
    x: 0.6,
    y: 0.4,
    w: 11.8,
    h: 0.5,
    fontSize: 22,
    bold: true,
    color: "0F172A",
  });

  const bodyLines = [];
  if (slideData.paragraphs.length) {
    bodyLines.push(...slideData.paragraphs.slice(0, 3));
  }
  if (slideData.bullets.length) {
    bodyLines.push(...slideData.bullets.slice(0, 6).map((item) => `• ${item}`));
  }
  if (!bodyLines.length) {
    bodyLines.push("待补充本页文案与素材说明。");
  }

  slide.addText(bodyLines.join("\n"), {
    x: 0.9,
    y: 1.3,
    w: 11.0,
    h: 4.8,
    fontSize: 16,
    color: "334155",
    breakLine: false,
    margin: 0.05,
    valign: "top",
  });

  slide.addText(`Slide ${index + 1}`, {
    x: 11.1,
    y: 6.6,
    w: 1.0,
    h: 0.25,
    fontSize: 9,
    color: "94A3B8",
    align: "right",
  });
});

await pptx.writeFile({ fileName: absoluteOutput });
console.log(`Exported PPT: ${absoluteOutput}`);
