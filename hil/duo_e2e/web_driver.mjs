// Drive the web simulator (server/duo_web) with a fake-microphone WAV: connect, optionally double-tap,
// follow the page's log, take a screenshot, disconnect. Coordinator only.
//
//   DUO_E2E_NODE_MODULES=<dir> node hil/duo_e2e/web_driver.mjs <wav> <seconds> <out-dir> [tapAt,tapAt...]
//
// <out-dir> gets web.log (the page's log lines, each with the seconds since connect), web.png and
// played.json (when the WAV started, for the scorer). puppeteer-core is resolved from
// DUO_E2E_NODE_MODULES: the directory it was installed in, or that directory's node_modules.
// DUO_E2E_CHROME names another Chrome than /usr/bin/google-chrome.
//
// The page URL is read here, from ~/.config/duo-web.url. It is not an argument (process lists show
// argv), and it is never printed or written: everything this script emits goes through scrub().
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";

delete process.env.DEBUG;  // puppeteer's protocol trace would print the URL.

const usage = (why) => {
  console.error(`web_driver: ${why}\nusage: web_driver.mjs <wav> <seconds> <out-dir> [tapAt,tapAt...]`);
  process.exit(2);
};
const [wav, seconds, outDir, taps = ""] = process.argv.slice(2);
const tapTimes = taps ? taps.split(",").map(Number) : [];
if (!wav || !outDir || !(Number(seconds) > 0) || tapTimes.some(Number.isNaN)) {
  usage("a WAV, a duration in seconds and an output directory are required");
}
if (!fs.existsSync(wav)) usage("the WAV does not exist");
const modules = process.env.DUO_E2E_NODE_MODULES;
if (!modules) usage("DUO_E2E_NODE_MODULES must name the directory puppeteer-core is installed in");

let url, parsed;
try {
  url = fs.readFileSync(path.join(os.homedir(), ".config", "duo-web.url"), "utf8").trim();
  parsed = new URL(url);
} catch {
  usage("~/.config/duo-web.url must hold the simulator URL");  // The error itself may quote the URL.
}
// Every form the URL takes in an error or on the page: whole, origin, host, the secret path, a query.
const secrets = [url, parsed.href, parsed.origin, parsed.host, parsed.hostname, parsed.search.slice(1),
                 ...parsed.pathname.split("/"), ...parsed.searchParams.values()]
  .filter((part) => part.length >= 4).sort((a, b) => b.length - a.length);
let leaked = false;  // Something to emit held the URL: if that was the page, a screenshot would show it.
const scrub = (text) => {
  const clean = secrets.reduce((kept, secret) => kept.split(secret).join("<url>"), String(text));
  leaked ||= clean !== String(text);
  return clean;
};

fs.mkdirSync(outDir, { recursive: true });
const logFile = path.join(outDir, "web.log");
fs.writeFileSync(logFile, "");
const emit = (line) => fs.appendFileSync(logFile, scrub(line) + "\n");
const fail = (error) => {
  const message = scrub(`web_driver: ${error?.message ?? error}`);
  emit(message);
  console.error(message);
  process.exit(1);
};
process.on("uncaughtException", fail);
process.on("unhandledRejection", fail);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let browser;
try {
  const require = createRequire(path.join(path.resolve(modules), "from-here.js"));
  browser = await require("puppeteer-core").launch({
    executablePath: process.env.DUO_E2E_CHROME || "/usr/bin/google-chrome", headless: true,
    defaultViewport: { width: 1000, height: 720 },  // Wide enough for the face beside the device panel.
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
           `--use-file-for-fake-audio-capture=${path.resolve(wav)}%noloop`,
           "--autoplay-policy=no-user-gesture-required"],
  });
  // A context of its own keeps history and cache in memory: the profile on disk never holds the URL.
  const page = await (await browser.createBrowserContext()).newPage();
  page.on("pageerror", (error) => emit(`PAGEERROR ${error?.message ?? error}`));
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById("log").textContent.includes("device session"));
  await page.click("#connect");
  // The page asks for the microphone on connect, and the fake device starts the WAV then.
  const t0 = Date.now();
  fs.writeFileSync(path.join(outDir, "played.json"),
                   JSON.stringify([{ t: t0 / 1000, wav_s: 0, len_s: Number(seconds) }]) + "\n");
  const since = () => ((Date.now() - t0) / 1000).toFixed(1);
  const follow = async (at) => {
    // The page keeps only its newest 600 lines, so mark the ones taken instead of counting them.
    const lines = await page.$$eval("#log > div:not([data-seen])",
                                    (rows) => rows.map((row) => { row.dataset.seen = "1"; return row.textContent; }));
    for (const line of lines) emit(`${at}\t${line}`);
  };
  // No session, no run: say so now instead of logging silence for the length of the WAV.
  await page.waitForFunction(() => document.getElementById("state").textContent === "ready", { timeout: 30000 })
    .catch(async () => {
      await follow(since());
      throw new Error("the simulator was not ready within 30 s of connect: is the bot up?");
    });
  const box = await (await page.$("#face")).boundingBox();
  const x = box.x + box.width / 2, y = box.y + box.height / 2;
  while (Number(since()) < Number(seconds)) {
    const at = since();
    if (tapTimes.length && Number(at) >= tapTimes[0]) {
      tapTimes.shift();
      await page.mouse.click(x, y);  // Two presses inside the page's 400 ms double-tap window.
      await sleep(120);
      await page.mouse.click(x, y);
      emit(`${at}\tTAP`);
    }
    await follow(at);
    await sleep(200);
  }
  const [state, character, counts] = await Promise.all(
    ["#state", "#character", "#counts"].map((id) => page.$eval(id, (element) => element.textContent)));
  emit(`STATE ${state} | ${character} | ${counts}`);
  console.log(scrub(`web_driver: ${state} | ${character} | ${counts}`));
  if (leaked) emit("SCREENSHOT skipped: a line above held the URL, so the page may show it");
  else await page.screenshot({ path: path.join(outDir, "web.png") });
  await page.click("#connect");  // Disconnect, and give the bot a second to see it.
  await sleep(1000);
} catch (error) {
  await browser?.close().catch(() => {});
  fail(error);
}
await browser.close();
