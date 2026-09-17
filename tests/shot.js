/**
 * 看板截图：起一个静态服务 → 无头 Chrome 打开 → 截全页图。
 * 用来做版式自检（冒烟测试只覆盖逻辑，不覆盖排版）。
 *
 * 用法： node tests/shot.js [输出文件名] [视口宽]
 */

const fs = require('fs');
const path = require('path');
const http = require('http');
const puppeteer = require('/Users/limx/.workbuddy/binaries/node/workspace/node_modules/puppeteer-core');

const ROOT = path.resolve(__dirname, '..');
const WEB = path.join(ROOT, 'web');
const OUT = path.join(ROOT, 'tests', process.argv[2] || 'shot-dashboard.png');
const WIDTH = parseInt(process.argv[3] || '1200', 10);
const SEL = process.argv[4] || '';
const PORT = 8231;

const MIME = { '.html': 'text/html; charset=utf-8', '.json': 'application/json; charset=utf-8',
               '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
               '.png': 'image/png', '.svg': 'image/svg+xml' };

const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
  const file = path.join(WEB, rel);
  if (!file.startsWith(WEB) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404); res.end('not found'); return;
  }
  res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
  res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise(r => server.listen(PORT, '127.0.0.1', r));
  const browser = await puppeteer.launch({
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    headless: true,
    userDataDir: '/tmp/pptr-shot-profile',
    args: ['--no-sandbox', '--disable-gpu', '--disable-crash-reporter', '--no-first-run',
           '--disable-dev-shm-usage', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: WIDTH, height: 1000, deviceScaleFactor: 2 });

  const errs = [];
  page.on('pageerror', e => errs.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });

  await page.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: 'networkidle0', timeout: 45000 });
  await new Promise(r => setTimeout(r, 1800));

  if (SEL) {
    const el = await page.$(SEL);
    if (!el) { console.error('✗ 找不到选择器 ' + SEL); await browser.close(); server.close(); process.exit(1); }
    await el.screenshot({ path: OUT });
  } else {
    await page.screenshot({ path: OUT, fullPage: true });
  }
  const h = await page.evaluate(() => document.body.scrollHeight);
  console.log(`✓ 截图 ${path.relative(ROOT, OUT)}  (${WIDTH}×${h})`);
  if (errs.length) {
    console.log('⚠ 页面报错：');
    errs.slice(0, 10).forEach(e => console.log('   ' + e));
  } else {
    console.log('  无 JS 报错');
  }

  await browser.close();
  server.close();
  process.exit(errs.length ? 1 : 0);
})().catch(e => { console.error('✗', e.message); server.close(); process.exit(1); });
