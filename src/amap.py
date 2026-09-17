"""高德 Web 服务 API 客户端。

只依赖 requests（标准 GET + JSON）。高德把业务错误放在 HTTP 200 的 body 里
（status=0 / info 是错误原因），所以不能只看 HTTP 状态码。
"""

import time
import re

import requests

BASE = "https://restapi.amap.com"


class AmapError(RuntimeError):
    pass


class AmapClient:
    source = "amap"

    def __init__(self, key: str, timeout: int = 20, retries: int = 3):
        if not key or key.startswith("PUT_"):
            raise AmapError(
                "缺少高德 Key。请到 https://lbs.amap.com 申请「Web服务」类型的 Key，"
                "然后设置环境变量 AMAP_KEY，或写入 .env 文件。"
            )
        self.key = key
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.call_count = 0

    def _get(self, path: str, params: dict) -> dict:
        q = dict(params)
        q["key"] = self.key
        q["output"] = "JSON"
        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(BASE + path, params=q, timeout=self.timeout)
                self.call_count += 1
                data = resp.json()
                if str(data.get("status")) == "1":
                    return data
                raise AmapError(
                    "高德返回错误：%s (infocode=%s)" % (data.get("info"), data.get("infocode"))
                )
            except AmapError as exc:
                # 业务错误重试一般没用（配额/权限问题居多），直接抛出更省心
                raise exc
            except Exception as exc:  # 网络抖动才重试
                last_err = exc
                if attempt < self.retries:
                    time.sleep(1.5 * attempt)
        msg = str(last_err)
        if self.key:
            msg = msg.replace(self.key, "***")
        msg = re.sub(r"([?&]key=)[^&\s]+", r"\1***", msg, flags=re.I)
        raise AmapError("请求失败 %s：%s" % (path, msg))

    # ---------- 路径规划 ----------

    def driving_v3(self, origin, destination, strategy=None, waypoints=None, extensions="all",
                   cartype=None, province=None, number=None):
        """路径规划（v3）。extensions=all 才会返回 tmcs 分段路况。"""
        p = {"origin": origin, "destination": destination, "extensions": extensions}
        if strategy is not None:
            p["strategy"] = strategy
        if waypoints:
            p["waypoints"] = ";".join(waypoints)
        if cartype is not None:
            p["cartype"] = int(cartype)
        if province:
            p["province"] = province
        if number:
            p["number"] = number
        return self._get("/v3/direction/driving", p)

    def driving_v5(self, origin, destination, strategy=None, waypoints=None,
                   show_fields="cost,tmcs"):
        """路径规划 2.0（v5）。部分账号无 show_fields 权限，调用方需自行降级。"""
        p = {"origin": origin, "destination": destination, "show_fields": show_fields}
        if strategy is not None:
            p["strategy"] = strategy
        if waypoints:
            p["waypoints"] = ";".join(waypoints)
        return self._get("/v5/direction/driving", p)

    # ---------- 地理编码 ----------

    def geocode(self, address, city=None):
        p = {"address": address}
        if city:
            p["city"] = city
        return self._get("/v3/geocode/geo", p)

    def regeo(self, location):
        return self._get("/v3/geocode/regeo", {"location": location})

    def place_text(self, keywords, city=None, citylimit=False):
        p = {"keywords": keywords}
        if city:
            p["city"] = city
            p["citylimit"] = str(citylimit).lower()
        return self._get("/v3/place/text", p)
