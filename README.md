# WayToHome · 深圳西丽 ↔ 衡阳

自动采集四条走廊的当前路况，并在浏览器中手动记录预测方案，比较出发时间与路线。不会自动生成未来预测，也不预留预测 API。

**运行截止：北京时间 2026 年 10 月 8 日 00:00。** 截止后本地采集拒绝继续执行，GitHub Actions 在下一次触发时自动停用采集工作流。代码和历史数据保留，不删除仓库。截止配置见 `project-lifecycle.json`。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 初次使用才复制；已有 .env 不要覆盖
cp .env.example .env
# 在 .env 中填写 AMAP_KEY
.venv/bin/python src/collect.py --if-due
.venv/bin/python src/analyze.py
.venv/bin/python -m http.server 8000 --directory web
```

打开 http://localhost:8000。采集脚本只负责当前路况，手动记录在页面录入。

## 手动方案怎么用

选择去返方向，录入预计出发时间、查询时间、行驶耗时、来源名称和主要道路。费用与备注可不填。所有时间按北京时间处理，不受浏览器所在时区影响。

记录可编辑、删除，并以 JSON 导出备份或导入另一设备。导入先预览，同 ID 内容冲突可选择保留本地或采用导入版本。文件最大 5 MB，记录最多 5000 条。

**记录仅存在当前浏览器，不上传服务器或公开仓库。** 更换浏览器、端口、设备或清除站点数据后，不会自动出现原记录，请先导出备份。存储读取异常时停止写入，避免覆盖原数据。

用最早出发、最晚出发、最晚抵达和休息时长筛选方案。默认超过查询时间 24 小时的记录需重新查询，可自行调整有效期。该有效期只是复核提醒，不代表预测准确性。待核验、未来查询时间、已过出发时间及非出发前查询记录不参加排名。

矩阵只显示有效且满足条件的最新记录，同列比较同一出发时刻；缺失处显示“未录入”。推荐仅指已录入方案中的比较结果。休息时间单独计入抵达，未知费用不推断免费。

## 自动采样与历史参考

保持四条走廊、去返方向和各自的高速主线路点。常规每 30 分钟采样，密集档为 15 分钟。当前排名只使用 45 分钟内的成功采样；浏览器也会检查时间，避免采集停跑后继续展示旧排名。

采样是高德对当时出发的估算，不是车辆实跑耗时。历史图按日历场景筛选，显示每条走廊样本数、日期数和小时覆盖。节假日名称来自 `holiday.names`，假日与调休日沿用 `config.yaml`；假期首日、中段、后段、节前及节间日期分别标记。后段是本项目分析分类，并不表示实际返程高峰一定发生。

分段基线只比较同走廊、同路线版本、同场景及相近小时。旧路线、夹具和仿真数据不进入正式历史分析。手动记录也不进入采样基线。原先经验外推的未来窗口不再输出或展示，兼容字段保持空列表。

## 三、申请高德 Key（约 5 分钟，免费）

1. 打开 <https://lbs.amap.com> → 右上角「注册」，用手机号注册
2. 登录后完成**个人认证**（支付宝扫码，几分钟通过）
3. 右上角进「控制台」→ 左侧「应用管理」→「我的应用」→「创建新应用」
4. 应用建好后点「添加 Key」：
   - **服务平台必须选「Web服务」**（不是 Web端 JS API，选错了会报 `INVALID_USER_SCODE`）
   - 名字随便填，比如 `route-monitor`
5. 复制生成的 Key，填进 `.env`：

```
AMAP_KEY=你复制的那串字符
```

按高德 2026 年公开定价页，符合非商业条件的个人认证开发者，基础 LBS 服务共享月配额为
15 万次，并注明自注册认证之日起享受 1 年免费月配额；最终以你的控制台为准。
本系统每轮消耗 **8 次调用**。30 分钟档约 384 次/天，15 分钟档约 768 次/天，
整月都跑密集档约 2.3 万次。价格和配额见 <https://lbs.amap.com/upgrade>。

---

## 四、部署到 GitHub Actions（推荐，电脑不用常开）

本机跑的问题是：Mac 关机就没数据了。丢到 GitHub Actions 上就不用管了。

1. 在 GitHub 新建一个**公开仓库**并把本目录推上去。GitHub Free 的私有仓库 Pages 不属于免费方案，
   而且私有仓库 Actions 有分钟数额度
2. 仓库 → **Settings → Secrets and variables → Actions → New repository secret**
   - Name 填 `AMAP_KEY`，Value 粘贴你的 Key
3. 同一页切到 **Variables**，添加 `ENABLE_PAGES`，值填 `true`
4. 仓库 → **Actions** 标签页 → 左侧「采集路况」→「Enable workflow」
5. 仓库 → **Settings → Pages**：Source 选 **GitHub Actions**

工作流在每小时的 07、22、37、52 分错峰唤醒，避开 GitHub 定时任务最拥挤的整刻；脚本再按
`config.yaml` 的 30/15 分钟两档判断是否调用高德。
采集、分析完成后，工作流直接发布 `web/`，不依赖机器人提交再次触发 Pages。

免费方案使用公开仓库，因此 `data/raw/` 中的起终点、路线和采样时间也会公开。
`noindex` 只能减少搜索引擎收录，不能提供访问控制。若这些数据不能公开，需要改用付费私有方案或其他带鉴权的托管方式。

**两个必须知道的事**：

- GitHub 的定时任务可能延迟，繁忙时也可能丢弃排队任务，因此这是准实时监测，不是严格的实时系统。
- 仓库**连续 60 天没有任何提交，GitHub 会自动停用定时任务**。本工作流每次跑都会提交数据，
  所以只要在跑就不会被停；但如果中途停很久，记得回仓库看一眼 Actions 页面。

工作流目前每 15 分钟唤醒一次，所以把 `interval_dense_minutes` 改到 15 分钟以下不会提高实际频率。

---

## 五、手机查看

部署成功后，直接用手机浏览器打开 GitHub Pages 地址。页面会每 5 分钟重新读取一次
`data.json`，也可以下拉刷新。需要更快打开时，把页面添加到浏览器收藏夹或手机主屏幕即可。

### iPhone 通知：Bark

使用 [Bark](https://github.com/Finb/Bark) 接收通知，不需要开发小程序，也不用让 Mac 常开。
程序通过 [Bark HTTP API](https://github.com/Finb/bark-server/blob/master/docs/API_V2.md)
发送普通通知；点击通知可以打开看板。

1. iPhone 安装 Bark，允许通知，复制 App 中的专属地址，形如 `https://api.day.app/你的设备密钥`。
2. GitHub 仓库 → **Settings → Secrets and variables → Actions → Secrets**，
   添加 `BARK_URL`，值填上述地址。也支持 App 复制的带测试标题/正文的地址，但不要带 `?` 查询参数。
3. 同一页面切到 **Variables**，添加 `DASHBOARD_URL`，值填你的 GitHub Pages HTTPS 看板地址（可选）。
4. **Actions → 测试 iPhone 推送 → Run workflow**，手动发一条消息，确认手机收到。

默认自动提醒日期为 **北京时间 2026/9/25 至 10/7 全天**，覆盖你的去返出行窗口：

| 条件 | 通知规则 |
|---|---|
| 推荐走廊预计耗时明显变化 | 比首次或上次已提醒采样增加/减少至少 30 分钟；参照最长保留 24 小时 |
| 备选路线明显更快 | 同轮高德估算比推荐走廊少至少 30 分钟；同一优势持续时只报一次 |
| 采集异常 | 连续 3 个不同采集轮次所有路线都失败，或最新采样超过 45 分钟 |
| 异常恢复 | 已报过异常后，重新取得新鲜的有效路线时提醒一次 |

同一方向的通知至少间隔 **60 分钟**。第一次成功采样建立耗时参照，不会凭空生成变化提醒；
若此时备选路线已明显更快，仍可提示。这里只比较高德**当前预计耗时**，不把经验预测的出发窗口
当成确定结论推送，也不把这个短期参照叫“平常耗时”。路线版本改变、道路序列改变或里程变化
超过 2% 时重新建立参照。旧版本、仿真数据和过期数据不触发交通变化提醒。

通知状态保存在 `data/notification-state.json`，随采样一起提交，用于跨运行去重；其中不含 Bark 密钥。
不要把 `BARK_URL` 放进 `config.yaml`、`web/` 或公开仓库。发送失败不记为已送达，下一轮重新判断；
网络超时但服务端已接收，或通知成功后状态提交失败时，仍可能出现重复通知。

未设置 `BARK_URL` 会跳过推送，不影响采集或看板。普通通知遵循 iPhone 的通知/专注模式设置。
采集异常提醒依赖工作流仍能执行；GitHub 定时任务完全停跑时，这个任务无法给自己发停机告警。

本机使用时，把 `BARK_URL` 和可选的 `DASHBOARD_URL` 添加到已有 `.env`，不要覆盖原来的 `AMAP_KEY`：

```bash
.venv/bin/python src/notify.py --test             # 发送一条测试通知
.venv/bin/python src/notify.py --test --dry-run   # 只预览测试文案，不发消息
.venv/bin/python src/notify.py --dry-run          # 预览当前是否触发，不改状态
```

本机定时运行须依次执行 `collect.py`、`analyze.py`、`notify.py`。
`collect.py --loop` 只采集，不自动分析或推送。日期与阈值都在 `config.yaml` 的 `notifications` 段调整。

---

## 配置与验证

走廊和途经点在 `trip.corridors` / `return_trip.corridors`，改路线需增加对应 `route_version`。频率在 `collect`，日历在 `holiday`，Bark 规则在 `notifications`。

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
node tests/test_manual.js
node tests/smoke_web.js
node tests/smoke_widget.js
node tests/browser_manual.js
```

浏览器测试需要 Playwright 和 Chrome。可通过 `PLAYWRIGHT_MODULE` 指定已安装的 Playwright 模块路径。测试使用独立临时浏览器，示例记录标注“非真实预测”，不会写入个人记录或正式采样。截图输出到 `tests/shot-manual-*.png`。

主要代码位于 `src/collect.py`、`src/analyze.py`、`src/calendar_tags.py`；手动记录模型与界面位于 `web/manual-model.js` 和 `web/manual.js`。

本地实现和测试不代表已发布到线上；GitHub Actions 部署步骤仍按上文配置执行。

详细设计见 [项目规划](docs/project-plan-v2.md)，历史调查见 [权限核验](docs/forecast-feasibility.md)。
