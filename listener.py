import re
import asyncio
import traceback
from typing import Dict, Any, Tuple
from astrbot.api import logger
from astrbot.api.message_components import Image, Plain, Node
from astrbot.api.event import MessageEventResult, MessageChain
from astrbot.api.all import *
from .data_manager import DataManager
from .bili_client import BiliClient
from .renderer import Renderer
from .utils import *
from .constant import LOGO_PATH


class DynamicListener:
    """
    负责后台轮询检查B站动态和直播，并推送更新。
    """

    def __init__(
        self,
        context: Context,
        data_manager: DataManager,
        bili_client: BiliClient,
        renderer: Renderer,
        interval_mins: float,
        rai: bool,
        node: bool,
    ):
        self.context = context
        self.data_manager = data_manager
        self.bili_client = bili_client
        self.renderer = renderer
        self.interval_mins = interval_mins
        self.rai = rai  # 非图文动态也可能需要这个配置
        self.node = node

    async def start(self):
        """启动后台监听循环。"""
        while True:
            await asyncio.sleep(60 * self.interval_mins)
            if self.bili_client.credential is None:
                logger.warning("bilibili sessdata 未设置，无法获取动态")
                continue

            all_subs = self.data_manager.get_all_subscriptions()
            for sub_user, sub_list in all_subs.items():
                for sub_data in sub_list:
                    try:
                        await self._check_single_up(sub_user, sub_data)
                    except Exception as e:
                        logger.error(
                            f"处理订阅者 {sub_user} 的 UP主 {sub_data.get('uid', '未知UID')} 时发生未知错误: {e}\n{traceback.format_exc()}"
                        )

    async def _check_single_up(self, sub_user: str, sub_data: Dict[str, Any]):
        """检查单个订阅的UP主是否有更新。"""
        uid = sub_data.get("uid")
        if not uid:
            return

        # 检查动态更新
        dyn = await self.bili_client.get_latest_dynamics(uid)
        if dyn:
            render_data, dyn_id = await self._parse_and_filter_dynamics(dyn, sub_data)
            if render_data:
                await self._handle_new_dynamic(sub_user, render_data)
                await self.data_manager.update_last_dynamic_id(sub_user, uid, dyn_id)
            elif dyn_id:  # 动态被过滤，只更新ID
                await self.data_manager.update_last_dynamic_id(sub_user, uid, dyn_id)

        # 检查直播状态
        if "live" in sub_data.get("filter_types", []):
            return
        lives = await self.bili_client.get_live_info(uid)
        if lives:
            await self._handle_live_status(sub_user, sub_data, lives)

    def _compose_plain_dynamic(
        self, render_data: Dict[str, Any], render_fail: bool = False
    ):
        """转换为纯文本消息链。"""
        name = render_data.get("name")
        summary = render_data.get("summary", "")
        prefix_fail = [Plain("渲染图片失败了 (´;ω;`)\n")] if render_fail else []
        ls = [
            *prefix_fail,
            Plain(f"📣 UP 主 「{name}」 发布了新图文动态:\n"),
            Plain(summary),
        ]
        for pic in render_data.get("image_urls", []):
            ls.append(Image.fromURL(pic))
        return ls

    async def _send_dynamic(
        self, sub_user: str, chain_parts: list, send_node: bool = False
    ):
        if self.node or send_node:
            qqNode = Node(
                uin=0,
                name="AstrBot",
                content=chain_parts,
            )
            await self.context.send_message(
                sub_user, MessageEventResult(chain=[qqNode])
            )
        else:
            await self.context.send_message(
                sub_user, MessageEventResult(chain=chain_parts).use_t2i(False)
            )

    async def _handle_new_dynamic(self, sub_user: str, render_data: Dict[str, Any]):
        """处理并发送新的动态通知。"""
        # 非图文混合模式
        if not self.rai and render_data.get("type") in (
            "DYNAMIC_TYPE_DRAW",
            "DYNAMIC_TYPE_WORD",
        ):
            ls = self._compose_plain_dynamic(render_data)
            await self._send_dynamic(sub_user, ls)
        # 默认渲染成图片
        else:
            img_path = await self.renderer.render_dynamic(render_data)
            if img_path:
                url = render_data.get("url", "")
                ls = [
                    Image.fromFileSystem(img_path),
                    Plain(f"{url}"),
                ]
                if self.node:
                    await self._send_dynamic(sub_user, ls, send_node=True)
                else:
                    await self.context.send_message(
                        sub_user, MessageEventResult(chain=ls).use_t2i(False)
                    )
            else:
                logger.error("渲染图片失败，尝试发送纯文本消息")
                ls = self._compose_plain_dynamic(render_data, render_fail=True)
                await self._send_dynamic(sub_user, ls, send_node=True)

    async def _handle_live_status(self, sub_user: str, sub_data: Dict, live_info: Dict):
        """处理并发送直播状态变更通知。"""
        is_live = sub_data.get("is_live", False)
        live_room = (
            live_info.get("live_room", {}) or live_info.get("live_room:", {}) or {}
        )
        live_name = live_room.get("title", "Unknown")
        user_name = live_info["name"]
        cover_url = live_room.get("cover", "")
        link = live_room.get("url", "Unknown")

        render_data = await create_render_data()
        render_data["name"] = "AstrBot"
        render_data["avatar"] = await image_to_base64(LOGO_PATH)
        render_data["title"] = live_name
        render_data["url"] = link
        render_data["image_urls"] = [cover_url]

        if live_room.get("liveStatus", "") and not is_live:
            render_data["text"] = f"📣 你订阅的UP 「{user_name}」 开播了！"
            await self.data_manager.update_live_status(sub_user, sub_data["uid"], True)
        if not live_room.get("liveStatus", "") and is_live:
            render_data["text"] = f"📣 你订阅的UP 「{user_name}」 下播了！"
            await self.data_manager.update_live_status(sub_user, sub_data["uid"], False)
        if render_data["text"]:
            render_data["qrcode"] = await create_qrcode(link)
            img_path = await self.renderer.render_dynamic(render_data)
            if img_path:
                await self.context.send_message(
                    sub_user,
                    MessageChain().file_image(img_path).message(render_data["url"]),
                )
            else:
                text = "\n".join(filter(None, render_data.get("text", "").split("\n")))
                await self.context.send_message(
                    sub_user,
                    MessageChain()
                    .message("渲染图片失败了 (´;ω;`)")
                    .message(text)
                    .url_image(cover_url),
                )

    async def _parse_and_filter_dynamics(
        self, dyn: Dict, data: Dict
    ) -> Tuple[Any, Any]:
        """
        解析并过滤动态。
        """
        uid, last = data["uid"], data["last"]
        filter_types = data.get("filter_types", [])
        filter_regex = data.get("filter_regex", [])
        items = dyn["items"]

        for item in items:
            if "modules" not in item:
                continue
            
            # 调试：输出动态的完整结构
            logger.info(f"动态 ID: {item.get('id_str')}")
            logger.info(f"动态类型: {item.get('type')}")
            logger.info(f"动态模块: {list(item.get('modules', {}).keys())}")
            if "module_tag" in item.get("modules", {}):
                logger.info(f"module_tag 内容: {item['modules']['module_tag']}")
            if "display" in item.get("modules", {}):
                logger.info(f"display 内容: {item['modules']['display']}")
            logger.info("=" * 50)
            
            # 过滤置顶
            if (
                "module_tag" in item["modules"]
                and "text" in item["modules"]["module_tag"]
                and item["modules"]["module_tag"]["text"] == "置顶"
            ):
                continue
            # 无新动态
            if item["id_str"] == last:
                return None, None

            dyn_id = item["id_str"]
            # 转发类型
            if item.get("type") == "DYNAMIC_TYPE_FORWARD":
                if "forward" in filter_types:
                    logger.info(f"转发类型在过滤列表 {filter_types} 中。")
                    return None, dyn_id  # 返回 None 表示不推送，但更新 dyn_id
                try:
                    content_text = item["modules"]["module_dynamic"]["desc"]["text"]
                except (TypeError, KeyError):
                    content_text = None
                if content_text and filter_regex:
                    for regex_pattern in filter_regex:
                        try:
                            if re.search(regex_pattern, content_text):
                                logger.info(f"转发内容匹配正则 {regex_pattern}。")
                                return None, dyn_id
                        except re.error as e:
                            continue
                render_data = await self.renderer.build_render_data(item)
                render_data["url"] = f"https://t.bilibili.com/{dyn_id}"
                render_data["qrcode"] = await create_qrcode(render_data["url"])

                render_forward = await self.renderer.build_render_data(
                    item["orig"], is_forward=True
                )
                if render_forward["image_urls"]:  # 检查列表是否非空
                    render_forward["image_urls"] = [
                        render_forward["image_urls"][0]
                    ]  # 保留第一项
                render_data["forward"] = render_forward
                return render_data, dyn_id
            elif item.get("type") in ("DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"):
                # 图文类型过滤
                if "draw" in filter_types:
                    logger.info(f"图文类型在过滤列表 {filter_types} 中。")
                    return None, dyn_id

                major = (
                    item.get("modules", {}).get("module_dynamic", {}).get("major", {})
                )
                if major.get("type") == "MAJOR_TYPE_BLOCKED":
                    logger.info(f"图文动态 {dyn_id} 为充电专属。")
                    return None, dyn_id
                opus = major["opus"]
                summary_text = opus["summary"]["text"]

                if (
                    opus["summary"]["rich_text_nodes"][0].get("text") == "互动抽奖"
                    and "lottery" in filter_types
                ):
                    logger.info(f"互动抽奖在过滤列表 {filter_types} 中。")
                    return None, dyn_id
                if filter_regex:  # 检查列表是否存在且不为空
                    for regex_pattern in filter_regex:
                        try:
                            if re.search(regex_pattern, summary_text):
                                logger.info(
                                    f"图文动态 {dyn_id} 的 summary 匹配正则 '{regex_pattern}'。"
                                )
                                return None, dyn_id  # 匹配到任意一个正则就返回
                        except re.error as e:
                            continue  # 如果正则表达式本身有误，跳过这个正则继续检查下一个
                render_data = await self.renderer.build_render_data(item)
                return render_data, dyn_id
            elif item.get("type") == "DYNAMIC_TYPE_AV":
                # 视频类型过滤
                if "video" in filter_types:
                    logger.info(f"视频类型在过滤列表 {filter_types} 中。")
                    return None, dyn_id
                render_data = await self.renderer.build_render_data(item)
                return render_data, dyn_id
            elif item.get("type") == "DYNAMIC_TYPE_ARTICLE":
                # 文章类型过滤
                if "article" in filter_types:
                    logger.info(f"文章类型在过滤列表 {filter_types} 中。")
                    return None, dyn_id
                is_blocked = (
                    item["modules"]["module_dynamic"]["major"]["type"]
                    == "MAJOR_TYPE_BLOCKED"
                )
                major = (
                    item.get("modules", {}).get("module_dynamic", {}).get("major", {})
                )
                if major.get("type") == "MAJOR_TYPE_BLOCKED":
                    logger.info(f"文章 {dyn_id} 为充电专属。")
                    return None, dyn_id
                render_data = await self.renderer.build_render_data(item)
                return render_data, dyn_id
            else:
                return None, None

        return None, None
