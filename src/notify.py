"""Bark 通知：只根据当前版本的高德采样触发，不使用经验预测。

python src/notify.py             # 采集后执行；没有 BARK_URL 就跳过
python src/notify.py --dry-run   # 预览，不联网、不修改通知状态
python src/notify.py --test      # 明确发送一条测试通知，不受日期窗口限制
"""

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=8))
LABELS = {"outbound": "去程", "return": "返程"}


class NotificationError(Exception):
    """消息必须可安全写入公开日志，不含密钥或服务器响应正文。"""


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def read_records(raw_dir):
    records = []
    for path in sorted(raw_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    records.append(rec)
            except ValueError:
                continue
    return records


def current_items(rec, trip):
    ids = {c["id"] for c in trip["corridors"]}
    version = int(trip.get("route_version", 1))
    return {c["id"]: c for c in rec.get("corridors", [])
            if c.get("id") in ids and c.get("ok") is True
            and c.get("route_version", rec.get("route_version", 1)) == version
            and positive(c.get("duration_min")) and positive(c.get("distance_km"))}


def anchor(item, at):
    roads = json.dumps(item.get("roads") or [], ensure_ascii=False).encode("utf-8")
    return {"at": at, "duration_min": float(item["duration_min"]),
            "distance_km": float(item["distance_km"]),
            "roads_hash": hashlib.sha256(roads).hexdigest()}


def comparable(before, after):
    # 道路序列或里程变化时重新建立参照，避免把改道当成堵车。
    return (before.get("roads_hash") == after["roads_hash"]
            and positive(before.get("distance_km"))
            and abs(after["distance_km"] / before["distance_km"] - 1) <= 0.02)


def duration(minutes):
    hours, mins = divmod(round(float(minutes)), 60)
    return "%d小时%02d分" % (hours, mins)


def evaluate(records, cfg, direction, previous, now):
    """返回 (可选通知, 接受该结果后保存的状态)。不发网络请求。

    有通知时，调用者必须等 Bark 确认成功后才保存；冷却中的条件保留到下一轮判断。
    """
    trip = cfg["trip" if direction == "outbound" else "return_trip"]
    settings = cfg.get("notifications", {})
    version = int(trip.get("route_version", 1))
    state = copy.deepcopy(previous) if previous.get("route_version") == version else {}
    state["route_version"] = version
    # 同一时间戳算一轮；夹具、仿真、旧版本和明显来自未来的数据全部排除。
    rounds = {}
    for rec in records:
        at = timestamp(rec.get("ts"))
        if (rec.get("direction") == direction and rec.get("source") == "amap"
                and rec.get("route_version", 1) == version and at
                and at <= now + timedelta(minutes=5)):
            rounds[at] = rec
    if not rounds:
        return None, state
    ordered = sorted(rounds)
    at = ordered[-1]
    rec = rounds[at]
    items = current_items(rec, trip)
    max_age = float(cfg.get("analysis", {}).get("max_current_age_minutes", 45))
    stale = (now - at).total_seconds() / 60 > max_age
    failures = 0
    for t in reversed(ordered):
        if current_items(rounds[t], trip):
            break
        failures += 1
    incident = stale or failures >= int(settings.get("failure_rounds", 3))
    fresh = bool(items) and not stale
    proposed = copy.deepcopy(state)
    title, lines = None, []
    prefix = LABELS[direction]

    if incident:
        if not state.get("incident"):
            title = prefix + "路况采集异常"
            detail = ("最近一次采样距今已超过%d分钟。" % max_age if stale else
                      "已连续%d轮未取得任何有效路线。" % failures)
            lines = [detail, "当前预计耗时暂不可用，请打开高德核对。"]
            proposed["incident"] = True
    elif fresh:
        main_cfg = next((c for c in trip["corridors"] if c.get("recommended")),
                        trip["corridors"][0])
        main = items.get(main_cfg["id"])
        if state.get("incident"):
            title = prefix + "路况采集恢复"
            lines = ["已重新取得高德路线数据。"]
            if main:
                lines.append("%s当前预计%s。" % (main_cfg["short"], duration(main["duration_min"])))
                proposed["anchor"] = anchor(main, rec["ts"])
            proposed["incident"] = False
        elif main:
            current = anchor(main, rec["ts"])
            before = state.get("anchor", {})
            before_at = timestamp(before.get("at"))
            route_changed = (rec.get("route_signature_checked")
                             and rec.get("route_signature_similarity", 1) < 0.6)
            # 最多与 24 小时内的首次/上次已提醒采样比较；不称它为历史常态。
            if (not before_at or at - before_at > timedelta(hours=24)
                    or not comparable(before, current) or route_changed):
                state["anchor"] = proposed["anchor"] = current
                before = current
                before_at = at
            delta = current["duration_min"] - before["duration_min"]
            if abs(delta) >= float(settings.get("eta_change_minutes", 30)):
                lines.append("%s当前预计%s，较%s采样%s%d分钟。" % (
                    main_cfg["short"], duration(current["duration_min"]),
                    before_at.astimezone(TZ).strftime("%m/%d %H:%M"),
                    "增加" if delta > 0 else "减少", round(abs(delta))))
                proposed["anchor"] = current
            fastest_id = min(items, key=lambda cid: float(items[cid]["duration_min"]))
            best = items[fastest_id]
            saving = current["duration_min"] - float(best["duration_min"])
            alternative = (fastest_id if fastest_id != main_cfg["id"]
                           and saving >= float(settings.get("alternative_saving_minutes", 30))
                           and not route_changed else None)
            if alternative is None:
                state["alternative_id"] = proposed["alternative_id"] = None
            elif alternative != state.get("alternative_id"):
                best_cfg = next(c for c in trip["corridors"] if c["id"] == alternative)
                lines.append("%s当前预计%s，比%s少约%d分钟；出发前在高德复核走法。" % (
                    best_cfg["short"], duration(best["duration_min"]),
                    main_cfg["short"], round(saving)))
                proposed["alternative_id"] = alternative
            if lines:
                title = prefix + "路况变化"
    if not title:
        return None, state
    last_sent = timestamp(state.get("last_sent_at"))
    if last_sent and (now - last_sent).total_seconds() < float(settings.get("cooldown_minutes", 60)) * 60:
        return None, state
    lines.append("采样于%s（北京时间）。" % at.astimezone(TZ).strftime("%m/%d %H:%M"))
    proposed["last_sent_at"] = now.isoformat(timespec="seconds")
    return {"title": title, "body": "\n".join(lines)}, proposed


def active(settings, now):
    for name, is_start in (("active_start", True), ("active_end", False)):
        if settings.get(name):
            limit = timestamp(settings[name])
            if limit is None:
                raise NotificationError("通知日期必须包含时区，请检查 config.yaml。")
            if (is_start and now < limit) or (not is_start and now >= limit):
                return False
    return settings.get("enabled", True)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_bark(bark_url, message, dashboard_url=""):
    # 接受 Bark 复制的基础地址或带测试标题/正文的地址；密钥只放在 POST 正文中。
    try:
        url = parse.urlsplit(bark_url.strip())
        key = url.path.strip("/").split("/")[0]
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or not re.fullmatch(r"[A-Za-z0-9_-]+", key)
                or key.startswith("PUT_YOUR_") or key == "push"):
            raise ValueError()
        endpoint = "https://%s/push" % url.netloc
    except ValueError:
        raise NotificationError("BARK_URL 应为 https://api.day.app/你的设备密钥（不带查询参数）。") from None
    payload = dict(message, device_key=key, group="waytohome", level="active", isArchive="1")
    if dashboard_url:
        dest = parse.urlsplit(dashboard_url)
        if dest.scheme != "https" or not dest.hostname or dest.username or dest.password:
            raise NotificationError("DASHBOARD_URL 必须是 HTTPS 看板地址。")
        payload["url"] = dashboard_url
    req = request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with request.build_opener(NoRedirect()).open(req, timeout=15) as response:
            result = json.loads(response.read(65536))
            if response.status != 200 or result.get("code") != 200:
                raise NotificationError("Bark 未确认接收，请检查设备密钥和服务状态。")
    except NotificationError:
        raise
    except (error.URLError, OSError, ValueError, AttributeError, HTTPException):
        # 不输出异常对象：某些服务会在 URL、错误信息或响应体中回显设备密钥。
        raise NotificationError("Bark 请求失败，未更新已发送状态；下次运行会重新判断。") from None


def read_state(path):
    if not path.exists():
        return {"schema": 1, "directions": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (state.get("schema") != 1 or not isinstance(state.get("directions"), dict)
                or any(not isinstance(s, dict) for s in state["directions"].values())):
            raise ValueError()
        return state
    except (ValueError, AttributeError):
        raise NotificationError("通知状态文件无效，请检查 data/notification-state.json，避免重复推送。") from None


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(cfg, records, state_path, bark_url, dashboard_url, now, dry_run=False, sender=send_bark):
    if not active(cfg.get("notifications", {}), now):
        print("当前不在通知日期窗口内，或通知已关闭。")
        return 0
    if not bark_url and not dry_run:
        print("未配置 BARK_URL，跳过推送（采集和看板不受影响）。")
        return 0
    state = read_state(state_path)
    errors = 0
    for direction in LABELS:
        if direction == "return" and not cfg.get("return_trip"):
            continue
        message, updated = evaluate(records, cfg, direction,
                                    state["directions"].get(direction, {}), now)
        if dry_run:
            print(json.dumps(message, ensure_ascii=False) if message else LABELS[direction] + "：暂无提醒。")
            continue
        if message:
            try:
                sender(bark_url, message, dashboard_url)
                print(LABELS[direction] + "：Bark 服务已接收通知。")
            except NotificationError as exc:
                print(str(exc), file=sys.stderr)
                errors += 1
                continue
        state["directions"][direction] = updated
        # 每个方向单独保存，另一方向发送失败时也不丢失去重状态。
        write_state(state_path, state)
    return 1 if errors else 0


def main():
    from collect import load_config, load_env_file

    ap = argparse.ArgumentParser(description="通过 Bark 发送 iPhone 路况提醒")
    ap.add_argument("--dry-run", action="store_true", help="不联网、不写状态")
    ap.add_argument("--test", action="store_true", help="发送测试通知，忽略启用开关和日期窗口")
    args = ap.parse_args()
    load_env_file()
    cfg = load_config()
    bark_url = os.environ.get("BARK_URL", "").strip()
    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()
    if args.test:
        message = {"title": "路况监测 · 测试通知", "body": "Bark 推送已接通。配置看板地址后，点击通知即可查看深圳西丽 ↔ 衡阳路况。"}
        if args.dry_run:
            print(json.dumps(message, ensure_ascii=False))
        elif not bark_url:
            raise NotificationError("请先在 .env 或 GitHub Actions Secret 中配置 BARK_URL。")
        else:
            send_bark(bark_url, message, dashboard_url)
            print("Bark 服务已接收测试通知，请在 iPhone 上检查送达。")
        return 0
    return run(cfg, read_records(ROOT / "data" / "raw"), ROOT / "data" / "notification-state.json",
               bark_url, dashboard_url, datetime.now(TZ), args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except NotificationError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
