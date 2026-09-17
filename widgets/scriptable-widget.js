// 国庆长途路况 · iOS 小组件（Scriptable）—— 4 条候选走廊版
//
// 为什么用 Scriptable 而不是 Xcode 写原生小组件：
//   1. 免费，App Store 直接装，不需要 99 美元的开发者账号
//   2. 不用装 Xcode、不用写 Swift
//   3. 原生小组件受 WidgetKit 预算限制（24 小时 40~70 次刷新，15~60 分钟一次），
//      而 Scriptable 小组件被点击时会立刻重跑脚本 —— 等于白送一个「手动实时刷新」按钮
//
// 用法：
//   1. App Store 装 Scriptable（免费）
//   2. 打开 Scriptable → 右上角 + → 把本文件全部内容粘进去 → 改下面的 CONFIG → 命名保存
//   3. 长按桌面空白处 → 左上角 + → 搜 Scriptable → 选合适尺寸 → 添加
//   4. 长按刚加的小组件 → 编辑小组件 → Script 选你刚保存的脚本
//   5. 想立刻刷新就点一下小组件

const CONFIG = {
  // 把 USER/REPO 换成你自己的。仓库是公开的话直接可用。
  url: "https://raw.githubusercontent.com/USER/REPO/main/web/data.json",
  // 想离线也能看：把 web/data.json 丢进 iCloud Drive 的 Scriptable 文件夹，然后填文件名
  localFile: null,
  // "outbound" 去程 / "return" 返程
  direction: "outbound",
  // 主数字显示哪条："fastest" 此刻最快的那条 / "recommended" 配置里标注的推荐走廊
  focus: "fastest",
};

// ---------------- 取数 ----------------

async function loadData() {
  if (CONFIG.localFile) {
    const fm = FileManager.iCloud();
    const p = fm.joinPath(fm.documentsDirectory(), CONFIG.localFile);
    if (fm.fileExists(p)) {
      if (!fm.isFileDownloaded(p)) await fm.downloadFileFromiCloud(p);
      return JSON.parse(fm.readString(p));
    }
  }
  const req = new Request(CONFIG.url + "?t=" + Date.now());
  req.timeoutInterval = 20;
  return await req.loadJSON();
}

// ---------------- 工具 ----------------

const hm = s => (s ? s.slice(5, 16).replace("T", " ") : "—");
const h1 = m => (m / 60).toFixed(1);
const pad = n => String(n).padStart(2, "0");

const RAMP = { good: "#0F6E56", mid: "#8A5C0E", bad: "#8C3A2E" };
const STATUS_COLOR = {
  "畅通": "#4E9E5B", "缓行": "#D9A036",
  "拥堵": "#D9604F", "严重拥堵": "#A22C2C", "未知": "#C9C7C1",
};
const PALETTE = ["#1F5FA8", "#D9A036", "#D9604F", "#7A6FA8"];
const coColor = c => PALETTE[((c && c.idx) || 0) % PALETTE.length];

function ratioColor(r) {
  if (r == null) return Color.gray();
  if (r < 1.15) return new Color(RAMP.good);
  if (r < 1.45) return new Color(RAMP.mid);
  return new Color(RAMP.bad);
}

// 一段里「不畅」的里程占比 —— 比取占比最高的状态诚实
function jamShare(mix) {
  if (!mix) return 0;
  return 1 - (mix["畅通"] || 0);
}
function mixColor(mix) {
  const s = jamShare(mix);
  if (s < 0.08) return new Color(STATUS_COLOR["畅通"]);
  if (s < 0.35) return new Color(STATUS_COLOR["缓行"]);
  if (s < 0.6) return new Color(STATUS_COLOR["拥堵"]);
  return new Color(STATUS_COLOR["严重拥堵"]);
}

function addRow(stack, parts) {
  const row = stack.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
  for (const p of parts) {
    if (p.spacer) { row.addSpacer(p.spacer); continue; }
    const t = row.addText(p.t);
    t.font = p.font || Font.systemFont(11);
    t.textColor = p.color || Color.gray();
    if (p.lineLimit) t.lineLimit = p.lineLimit;
    if (p.scale) t.minimumScaleFactor = p.scale;
  }
  return row;
}

// ---------------- 渲染 ----------------

function build(data, family) {
  const d = data[CONFIG.direction] || data.outbound;
  const w = new ListWidget();
  w.setPadding(12, 14, 12, 14);
  w.backgroundColor = Color.white();

  const cos = (d && d.corridors) || [];
  if (!cos.length) {
    w.addText("还没有数据").textColor = Color.gray();
    return w;
  }

  const nowTable = cos.filter(c => c.now && c.now.ok && c.now.duration_min)
    .slice().sort((a, b) => a.now.duration_min - b.now.duration_min);
  const fastest = nowTable[0] || null;
  const recommended = cos.find(c => c.recommended) || cos[0];
  const focus = (CONFIG.focus === "recommended" ? recommended : (cos.find(c => c.id === (d.fastest_now)) || (fastest || recommended)));
  const fNow = focus.now || {};
  const fBl = focus.baseline || {};
  const ratio = fBl.freeflow_min ? fNow.duration_min / fBl.freeflow_min : null;
  const cov = d.coverage || {};
  const best = (d.best_overall || [])[0] || null;

  // 顶部：方向 + 更新时间
  const head = addRow(w, [
    { t: CONFIG.direction === "return" ? "返程" : "去程", font: Font.mediumSystemFont(11), color: Color.gray() },
    { t: "  " + hm(d.updated_at), font: Font.systemFont(10), color: Color.lightGray() },
  ]);
  head.addSpacer();
  const tag = head.addText(focus.recommended ? "★推荐" : "备选");
  tag.font = Font.systemFont(10);
  tag.textColor = new Color(coColor(focus));

  w.addSpacer(5);

  // 主数字：走这条走廊现在要多久
  const big = w.addStack();
  big.layoutHorizontally();
  big.centerAlignContent();
  const num = big.addText(h1(fNow.duration_min));
  num.font = Font.boldSystemFont(family === "small" ? 28 : 34);
  num.textColor = ratioColor(ratio);
  num.minimumScaleFactor = 0.6;
  const unit = big.addText(" 小时");
  unit.font = Font.systemFont(12);
  unit.textColor = Color.gray();
  big.addSpacer(6);
  const co = big.addText(focus.short || "");
  co.font = Font.mediumSystemFont(12);
  co.textColor = new Color(coColor(focus));
  co.lineLimit = 1;
  co.minimumScaleFactor = 0.65;

  // 副行
  let subTxt = "现在出发 · 畅通基准 " + h1(fBl.freeflow_min || 0) + "h";
  if (fastest && focus.id !== fastest.id) subTxt += " · 比最快慢 " + Math.round(focus.gap_min || 0) + " 分";
  const sub = w.addText(subTxt);
  sub.font = Font.systemFont(10);
  sub.textColor = Color.gray();

  // 小尺寸到此为止
  if (family === "small") {
    w.addSpacer();
    const f = w.addText((cov.days || 0) + " 天基线 · " + cos.length + " 条走廊");
    f.font = Font.systemFont(9);
    f.textColor = Color.lightGray();
    return w;
  }

  w.addSpacer(7);

  // 4 条走廊一览（各自用自己的畅通基准算颜色，才不是拿一条的路况套全部）
  nowTable.forEach(c => {
    const n = c.now || {};
    const ff = (c.baseline || {}).freeflow_min;
    const row = addRow(w, [
      { t: "●", font: Font.systemFont(8), color: new Color(coColor(c)) },
      { t: " " + (c.recommended ? "★" : " ") + (c.short || c.id), font: Font.systemFont(10.5),
        color: new Color(c.id === focus.id ? "#1D1D1F" : "#555555"), lineLimit: 1, scale: 0.7 },
    ]);
    row.addSpacer();
    const gapTxt = (c.gap_min == null || c.gap_min <= 0) ? "最快" : "+" + Math.round(c.gap_min) + "分";
    addRow(row, [
      { t: h1(n.duration_min) + "h", font: Font.mediumSystemFont(10.5),
        color: ratioColor(ff ? n.duration_min / ff : null) },
      { t: "  " + gapTxt, font: Font.systemFont(9.5),
        color: new Color(((c.gap_min == null || c.gap_min <= 0) ? RAMP.good : RAMP.bad)) },
    ]);
  });

  // 大尺寸：补最佳窗口 + 最堵断面
  if (family === "large") {
    w.addSpacer(6);
    if (best) {
      addRow(w, [
        { t: "最佳窗口 ", font: Font.systemFont(10), color: Color.gray() },
        { t: best.date.slice(5) + " " + pad(best.hour) + ":00 ", font: Font.mediumSystemFont(10.5), color: new Color("#1F5FA8") },
        { t: (best.corridor_short || "") + " " + best.est_h + "h", font: Font.systemFont(10.5), color: new Color("#444444"), lineLimit: 1, scale: 0.7 },
      ]);
      if (best.free_toll) {
        const r = w.addStack();
        r.layoutHorizontally();
        const b = r.addText("  到达落在免费时段");
        b.font = Font.systemFont(9.5);
        b.textColor = new Color(RAMP.good);
      }
    }
    const bins = (focus.bins || []).slice().sort((a, b2) => (a.eff || 1) - (b2.eff || 1));
    if (bins.length) {
      w.addSpacer(4);
      bins.slice(0, 2).forEach((b, i) => {
        addRow(w, [
          { t: (i + 1) + ". ", font: Font.systemFont(9.5), color: Color.lightGray() },
          { t: (b.road || "—") + "  ", font: Font.systemFont(10), color: new Color("#444444"), lineLimit: 1, scale: 0.7 },
          { t: (b.spd != null ? Math.round(b.spd) + "km/h" : ""), font: Font.systemFont(9.5), color: mixColor(b.mix) },
        ]);
      });
    }
  }

  w.addSpacer();

  // 页脚
  const foot = w.addText(
    (cov.days || 0) + " 天基线 · 成熟度 " + Math.round((cov.maturity || 0) * 100) + "%");
  foot.font = Font.systemFont(9);
  foot.textColor = Color.lightGray();

  return w;
}

// ---------------- 入口 ----------------

let widget;
try {
  const data = await loadData();
  widget = build(data, config.widgetFamily || "medium");
} catch (e) {
  widget = new ListWidget();
  widget.setPadding(12, 14, 12, 14);
  widget.backgroundColor = Color.white();
  const t = widget.addText("读取失败");
  t.font = Font.mediumSystemFont(13);
  t.textColor = new Color("#8C3A2E");
  const m = widget.addText(String(e.message || e).slice(0, 90));
  m.font = Font.systemFont(10);
  m.textColor = Color.gray();
}

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  await widget.presentMedium();
}
Script.complete();
