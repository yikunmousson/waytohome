/**
 * 看板冒烟测试：把 web/index.html 里的脚本抽出来，配上真实 data.json
 * 和一套最小 DOM 桩，跑一遍所有 render 函数，抓运行时错误。
 *
 * 比装 Chromium 轻得多，能覆盖 90% 的「字段名写错 / 空值崩溃」类问题。
 *
 * 用法： node tests/smoke_web.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
const data = JSON.parse(fs.readFileSync(path.join(ROOT, 'web', 'data.json'), 'utf8'));

// 抽出最后一个内联 <script>（前面的 CDN script 没有内容）
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) {
  console.error('✗ 没在 index.html 里找到内联脚本');
  process.exit(1);
}
const code = scripts[scripts.length - 1];

// ---- 最小 DOM 桩 ----
const touched = new Map();
function makeEl(id) {
  const el = {
    id,
    innerHTML: '',
    textContent: '',
    className: '',
    style: {},
    attrs: {},
    children: [],
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); },
    querySelectorAll() { return []; },
    remove() {},
    addEventListener() {},
    get parentElement() { return makeEl(id + ':parent'); },
    getBoundingClientRect() { return { width: 800, height: 280 }; },
  };
  return el;
}
const document = {
  getElementById(id) {
    if (!touched.has(id)) touched.set(id, makeEl(id));
    return touched.get(id);
  },
  createElement(tag) { return makeEl('created:' + tag); },
  querySelectorAll() { return []; },
};

let chartCount = 0;
class Chart {
  constructor(canvas, cfg) { this.canvas = canvas; this.cfg = cfg; this.destroyed = false; chartCount++; }
  destroy() { this.destroyed = true; }
}

// fetch 桩：返回真实 data.json
const fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });

const sandbox = {
  document, Chart, fetch, console,
  Math, JSON, Date, Object, Array, String, Number, Boolean, RegExp, Error,
  setTimeout, clearTimeout, isNaN, parseInt, parseFloat, Infinity, NaN, undefined,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);

let failed = false;
try {
  vm.runInContext(code, sandbox, { filename: 'web/index.html:inline' });
} catch (e) {
  console.error('✗ 脚本执行抛错：', e.message);
  console.error(e.stack.split('\n').slice(0, 6).join('\n'));
  failed = true;
}

// 等 fetch 的 then 链跑完，再切一次方向（覆盖两个方向的分支）
setTimeout(() => {
  if (failed) process.exit(1);

  // 只检查由 JS 渲染的区块（yTabs 等是页面里的静态 HTML，桩里拿不到）
  const critical = ['title', 'subtitle', 'verdict', 'cotable', 'mapSide', 'mapNote', 'dateTabs',
                    'corrLegend', 'hmTabs', 'heatmap', 'besttable', 'bestPerCo', 'segTabs', 'segMatrix',
                    'strip', 'stripLegend', 'roadlist', 'segtable', 'trendLegend', 'notes', 'foot'];
  const empty = critical.filter(id => {
    const el = touched.get(id);
    return !el || (!el.innerHTML && !el.textContent);
  });

  if (empty.length) {
    console.error('✗ 这些区块渲染后是空的：' + empty.join(', '));
    failed = true;
  }

  // 交互函数：切日期 / 切走廊 / 切纵轴口径 / 切方向 / 缩略图缺失时的降级
  const interactions = [
    ['pickDate',      () => sandbox.pickDate(2)],
    ['热力图切走廊',   () => sandbox.pickCorridor(sandbox.d().corridors[2].id, 'heat')],
    ['色带切走廊',     () => sandbox.pickCorridor(sandbox.d().corridors[1].id, 'seg')],
    ['纵轴切倍数',     () => sandbox.setYMode('x')],
    ['纵轴切耗时',     () => sandbox.setYMode('h')],
    ['切到返程',       () => sandbox.setDir('return')],
    ['缩略图缺图降级',  () => { const i = touched.get('mapImg'); if (i && i.onerror) i.onerror(); }],
  ];
  for (const [name, fn] of interactions) {
    try { fn(); } catch (e) {
      console.error(`✗ ${name} 抛错：`, e.message);
      failed = true;
    }
  }

  if (!failed) {
    const coCount = (data.outbound && data.outbound.corridors || []).length;
    console.log('✓ 看板脚本渲染通过');
    console.log('  区块渲染：' + critical.length + ' 个均有内容');
    console.log('  走廊数：' + coCount + '（1 推荐 + ' + (coCount - 1) + ' 备选）');
    console.log('  图表实例：' + chartCount + ' 次创建（含交互重建）');
    console.log('  交互：' + interactions.map(i => i[0]).join(' / ') + ' 均无异常');
    const v = touched.get('verdict').innerHTML.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    console.log('  结论：' + v.slice(0, 90));
  }
  process.exit(failed ? 1 : 0);
}, 300);
