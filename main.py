import asyncio
import json
import subprocess
import time
from pathlib import Path

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 统一放在插件目录，避免路径错位
STATE_FILE = Path(__file__).parent / "state.json"

@register("astrbot_plugin_cx_sclass", "assistant", "超星活动监控完整版", "1.2.0")
class CXPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        self.url = "https://hd.chaoxing.com/hd/api/activity/list/participate"
        self.interval = self.config.get("check_interval", 10)

        self.seen = set()           # 已推送过的活动ID
        self.subscribers = set()    # 订阅者

        self.session = aiohttp.ClientSession()
        self.cookie = ""

        # 定时保底刷新（秒）
        self.refresh_interval = self.config.get("refresh_interval", 21600)  # 默认6小时
        self.last_refresh = 0

    # ✅ 正确生命周期启动
    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        logger.info("CX插件启动")
        self.session = aiohttp.ClientSession()
        self.cookie = self.load_cookie()
        self.last_refresh = time.time()
        asyncio.create_task(self.loop())

    # 从 state.json 读取 cookie
    def load_cookie(self):
        if not STATE_FILE.exists():
            return ""
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        return "; ".join([f"{c['name']}={c['value']}" for c in cookies])

    # 调用外部脚本刷新 cookie（带日志与路径修复）
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

    # 请求接口（可切换 signUpAble）
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

                # ❗ HTTP层检测：非JSON 直接判定失效
                if "application/json" not in r.headers.get("Content-Type", ""):
                    return None

                return json.loads(text)
        except Exception as e:
            logger.error(f"请求异常: {e}")
            return None

    async def loop(self):
        while True:
            # ⏱ 定时保底刷新（避免长时间潜伏失效）
            if time.time() - self.last_refresh > self.refresh_interval:
                logger.info("⏱ 触发定时刷新Cookie")
                self.refresh_cookie()

            # 取“全部活动”和“可报名活动”各一份
            data_all = await self.fetch(signUpAble=False)
            data_signup = await self.fetch(signUpAble=True)

            # ❗ HTTP层失败 → 立即刷新
            if not data_all or not data_signup:
                logger.warning("⚠️ 请求异常，尝试刷新Cookie")
                self.refresh_cookie()
                await asyncio.sleep(5)
                continue

            # ❗ 结构保护
            try:
                records_all = data_all["data"]["records"]
                records_signup = data_signup["data"]["records"]
            except Exception:
                logger.error("返回结构异常")
                await asyncio.sleep(5)
                continue

            # ❗ 逻辑层鉴权检测（核心）
            # 正常：signup ⊆ all
            # 异常：signup == all → 很可能Cookie失效（退化为游客）
            if len(records_all) == len(records_signup) and len(records_all) != 0:
                logger.warning("⚠️ 检测到Cookie可能静默失效（逻辑层）")
                self.refresh_cookie()
                await asyncio.sleep(5)
                continue

            # ===== 正常流程：只基于“可报名活动”推送 =====
            new = []
            for i in records_signup:
                if i["id"] not in self.seen:
                    self.seen.add(i["id"])
                    new.append(i)

            if new:
                for sub in self.subscribers:
                    msg = "\n\n".join([
                        f"🆕 {i['name']}\n{i['previewUrl']}" for i in new
                    ])

                    message_chain = MessageChain().message(msg)
                    await self.context.send_message(sub, message_chain)

            await asyncio.sleep(self.interval)

    # ===== 测试指令 =====
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("测试检测")
    async def test_fetch(self, event: AstrMessageEvent):
        data = await self.fetch(signUpAble=True)
        if not data:
            msg = "❌ 获取失败（可能Cookie失效）"
        else:
            try:
                records = data["data"]["records"]
                msg = f"✅ 获取成功，共 {len(records)} 个活动\n"
                for i in records[:3]:
                    msg += f"\n{i['name']}"
            except Exception:
                msg = "❌ 数据结构异常"

        message_chain = MessageChain().message(msg)
        await self.context.send_message(event.unified_msg_origin, message_chain)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("刷新cookie")
    async def force_refresh(self, event: AstrMessageEvent):
        ok = self.refresh_cookie()
        message_chain = MessageChain().message("🔄 Cookie已刷新" if ok else "❌ 刷新失败")
        await self.context.send_message(event.unified_msg_origin, message_chain)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cookie状态")
    async def cookie_status(self, event: AstrMessageEvent):
        if self.cookie:
            msg = f"✅ Cookie已加载，长度: {len(self.cookie)}"
        else:
            msg = "❌ 当前没有Cookie"
        msg += f"\nstate路径: {STATE_FILE}"

        message_chain = MessageChain().message(msg)
        await self.context.send_message(event.unified_msg_origin, message_chain)