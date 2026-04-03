# AstrBot 超星活动监控（重构版 v4）
# 特性：
# - scheduler 调度（替代 while True，避免任务静默死亡）
# - config 持久化订阅（重启不丢）
# - Cookie 自动刷新（HTTP层 + 逻辑层 + 定时）
# - 首轮不推送历史
# - 更强健的异常隔离

import asyncio
import json
import subprocess
import time
from pathlib import Path

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

STATE_FILE = Path(__file__).parent / "state.json"

@register("astrbot_plugin_cx_sclass", "assistant", "超星活动监控完整版", "2.0.0")
class CXPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        self.url = "https://hd.chaoxing.com/hd/api/activity/list/participate"
        self.interval = self.config.get("check_interval", 10)

        self.session = None
        self.cookie = ""

        self.seen = set()  # 仅运行期去重（避免重复推送）

        self.refresh_interval = self.config.get("refresh_interval", 21600)
        self.last_refresh = 0

    # ================= 生命周期 =================
    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        logger.info("CX插件启动（v4调度版）")
        self.session = aiohttp.ClientSession()
        self.cookie = self.load_cookie()
        self.last_refresh = time.time()

        # 启动调度器（不会因异常停止）
        asyncio.create_task(self.scheduler())

    # ================= Cookie =================
    def load_cookie(self):
        if not STATE_FILE.exists():
            return ""
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        return "; ".join([f"{c['name']}={c['value']}" for c in cookies])

    def refresh_cookie(self):
        logger.info("刷新Cookie中...")
        import sys
        script_path = Path(__file__).parent / "refresh_cookie.py"

        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True
        )

        logger.info(f"stdout: {result.stdout}")
        if result.stderr:
            logger.error(f"stderr: {result.stderr}")

        if result.returncode != 0:
            logger.error("❌ refresh_cookie 执行失败")
            return False

        if not STATE_FILE.exists():
            logger.error("❌ state.json 未生成")
            return False

        self.cookie = self.load_cookie()
        self.last_refresh = time.time()
        logger.info("✅ Cookie刷新成功")
        return True

    # ================= 请求 =================
    async def fetch(self, signUpAble=True):
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://hd.chaoxing.com/?flag=second_classroom",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        payload = {
            "data": json.dumps({
                "pageNum": 1,
                "pageSize": 12,
                "topFid": 296090,
                "marketIds": [25239],
                "flag": "second_classroom",
                "signUpAble": signUpAble
            }),
            "pageNum": 1,
            "pageSize": 12
        }

        try:
            async with self.session.post(self.url, headers=headers, data=payload) as r:
                text = await r.text()

                # HTTP层检测
                if "application/json" not in r.headers.get("Content-Type", ""):
                    return None

                return json.loads(text)
        except Exception as e:
            logger.error(f"请求异常: {e}")
            return None

    # ================= 调度器 =================
    async def scheduler(self):
        while True:
            try:
                await self.monitor_job()
            except Exception as e:
                logger.error(f"监控任务异常: {e}")

            await asyncio.sleep(self.interval)

    # ================= 核心监控 =================
    async def monitor_job(self):
        # 定时刷新
        if time.time() - self.last_refresh > self.refresh_interval:
            logger.info("⏱ 定时刷新Cookie")
            self.refresh_cookie()

        data_all = await self.fetch(False)
        data_signup = await self.fetch(True)

        # HTTP异常
        if not data_all or not data_signup:
            logger.warning("⚠️ 请求失败，刷新Cookie")
            self.refresh_cookie()
            return

        try:
            records_all = data_all["data"]["records"]
            records_signup = data_signup["data"]["records"]
        except Exception:
            logger.error("数据结构异常")
            return

        # 逻辑层检测（静默失效）
        if len(records_all) == len(records_signup) and len(records_all) != 0:
            logger.warning("⚠️ Cookie疑似失效（逻辑层）")
            self.refresh_cookie()
            return

        # 首轮初始化（不推送历史）
        if not self.seen:
            self.seen = {i["id"] for i in records_signup}
            logger.info("初始化完成（跳过历史活动）")
            return

        # 检测新增
        new = []
        for i in records_signup:
            if i["id"] not in self.seen:
                self.seen.add(i["id"])
                new.append(i)

        if not new:
            return

        # 从配置读取订阅者（持久化）
        subs = self.config.get("subscribers", [])

        if not subs:
            return

        msg = "\n\n".join([
            f"🆕 {i['name']}\n{i['previewUrl']}" for i in new
        ])

        for sub in subs:
            await self.context.send_message(
                sub,
                MessageChain().message(msg)
            )

    # ================= 指令 =================

    @filter.command("订阅活动")
    async def sub(self, event: AstrMessageEvent):
        uid = event.unified_msg_origin
        subs = self.config.get("subscribers", [])

        if uid in subs:
            msg = "⚠️ 已经订阅过了"
        else:
            subs.append(uid)
            self.config["subscribers"] = subs
            self.config.save_config()
            msg = "✅ 订阅成功"

        await self.context.send_message(
            uid,
            MessageChain().message(msg)
        )

    @filter.command("取消订阅")
    async def unsub(self, event: AstrMessageEvent):
        uid = event.unified_msg_origin
        subs = self.config.get("subscribers", [])

        if uid in subs:
            subs.remove(uid)
            self.config["subscribers"] = subs
            self.config.save_config()
            msg = "✅ 已取消订阅"
        elif uid not in subs:
            msg = "⚠️ 你还没订阅"

        await self.context.send_message(uid, MessageChain().message(msg))

    # ================= 测试 =================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("测试检测")
    async def test_fetch(self, event: AstrMessageEvent):
        data = await self.fetch(True)

        if not data:
            msg = "❌ 获取失败"
        else:
            try:
                records = data["data"]["records"]
                msg = f"✅ {len(records)} 个活动"
            except Exception:
                msg = "❌ 解析失败"

        await self.context.send_message(
            event.unified_msg_origin,
            MessageChain().message(msg)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("刷新cookie")
    async def force_refresh(self, event: AstrMessageEvent):
        ok = self.refresh_cookie()
        msg = "🔄 已刷新" if ok else "❌ 刷新失败"
        await self.context.send_message(event.unified_msg_origin, MessageChain().message(msg))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cookie状态")
    async def cookie_status(self, event: AstrMessageEvent):
        msg = "✅ 已加载" if self.cookie else "❌ 无Cookie"
        msg += f"\n路径: {STATE_FILE}"

        await self.context.send_message(
            event.unified_msg_origin,
            MessageChain().message(msg)
        )
