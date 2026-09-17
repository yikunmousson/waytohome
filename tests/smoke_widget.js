/**
 * 小组件冒烟测试：把 scriptable-widget.js 放进一套 Scriptable API 桩里跑，
 * 用真实 data.json 渲染 small / medium / large 三种尺寸，抓字段名写错、空值崩溃。
 *
 * 用法： node tests/smoke_widget.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const code = fs.readFileSync(path.join(ROOT, 'widgets', 'scriptable-widget.js'), 'utf8');
const data = JSON.parse(fs.readFileSync(path.join(ROOT, 'web', 'data.json'), 'utf8'));

// ---- Scriptable API 桩 ----
const texts = [];

class Text {
  constructor(s) { this.s = String(s); this._font = null; this._color = null; this.lineLimit = 0; this.minimumScaleFactor = 1; texts.push(this); }
  set font(v) { this._font = v; } get font() { return this._font; }
  set textColor(v) { this._color = v; } get textColor() { return this._color; }
  set text(v) { this.s = String(v); } get text() { return this.s; }
}
class Stack {
  constructor() { this.children = []; }
  layoutHorizontally() { this.horiz = true; } layoutVertically() { this.horiz = false; }
  centerAlignContent() {} leftAlignContent() {} rightAlignContent() {}
  setPadding() {} addSpacer(n) { this.children.push({ spacer: n }); return null; }
  addText(s) { const t = new Text(s); this.children.push(t); return t; }
  addStack() { const s = new Stack(); this.children.push(s); return s; }
  addImage() { const i = {}; this.children.push(i); return i; }
}
class ListWidget extends Stack {
  constructor() { super(); this.backgroundColor = null; this._padding = null; }
  setPadding(t, l, b, r) { this._padding = [t, l, b, r]; }
  addText(s) { const t = new Text(s); this.children.push(t); return t; }
  addStack() { const s = new Stack(); this.children.push(s); return s; }
  addSpacer(n) { if (n !== undefined) this.children.push({ spacer: n }); }
  async presentMedium() {} async presentSmall() {} async presentLarge() {}
}
class Color {
  constructor(v) { this.v = v; }
  static white() { return new Color('#fff'); }
  static gray() { return new Color('#888'); }
  static lightGray() { return new Color('#ccc'); }
  static black() { return new Color('#000'); }
}
const Font = {
  systemFont: n => ({ family: 'system', size: n }),
  mediumSystemFont: n => ({ family: 'medium', size: n }),
  boldSystemFont: n => ({ family: 'bold', size: n }),
  lightSystemFont: n => ({ family: 'light', size: n }),
};
class Request {
  constructor(url) { this.url = url; this.timeoutInterval = 60; }
  async loadJSON() { return data; }
  async loadString() { return JSON.stringify(data); }
}
const FileManager = {
  iCloud: () => ({ documentsDirectory: () => '/tmp', joinPath: (a, b) => a + '/' + b,
                   fileExists: () => false, isFileDownloaded: () => true,
                   downloadFileFromiCloud: async () => {}, readString: () => '{}' }),
};
const Script = { setWidget() {}, complete() {} };

let widgetFamily = 'medium';
const config = {
  get widgetFamily() { return widgetFamily; },
  runsInWidget: true,
};

const sandbox = {
  ListWidget, Stack, Text, Color, Font, Request, FileManager, Script, config,
  console, Math, JSON, Date, Object, Array, String, Number, Boolean, RegExp, Error,
  Promise, isNaN, parseInt, parseFloat, Infinity, NaN, undefined, setTimeout,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

(async () => {
  let failed = false;
  const families = ['small', 'medium', 'large'];
  for (const fam of families) {
    widgetFamily = fam;
    texts.length = 0;
    try {
      // 脚本用了顶层 await，包一层 async IIFE 才能在 vm 里跑
      const wrapped = '(async () => {\n' + code + '\n})()';
      await vm.runInContext(wrapped, sandbox, { filename: 'scriptable-widget.js' });
    } catch (e) {
      console.error(`✗ ${fam} 渲染抛错：${e.message}`);
      console.error(e.stack.split('\n').slice(0, 5).join('\n'));
      failed = true;
      continue;
    }
    const strs = texts.map(t => t.s).filter(s => s && s.trim().length);
    if (process.env.VERBOSE) {
      console.log(`--- ${fam} 全部文本 ---\n` + strs.map((s, i) => `${i}: ${JSON.stringify(s)}`).join('\n'));
    }
    console.log(`${fam.padEnd(6)} ${String(strs.length).padStart(2)} 个文本 | ` + strs.slice(0, 8).join(' · ').slice(0, 110));
    if (strs.length < 3) { console.error(`✗ ${fam} 输出的文本太少，可能没渲染`); failed = true; }
    const bad = strs.filter(s => /undefined|NaN/.test(s));
    if (bad.length) {
      console.error(`✗ ${fam} 输出里出现了 undefined / NaN：` + bad.map(s => JSON.stringify(s)).join(', '));
      failed = true;
    }
  }
  if (!failed) console.log('✓ 小组件脚本三种尺寸渲染通过');
  process.exit(failed ? 1 : 0);
})();
