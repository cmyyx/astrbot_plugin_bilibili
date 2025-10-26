from ast import alias
import re
import json
import asyncio
from typing import List

from astrbot.core.star.filter.command import GreedyStr
from astrbot.api.all import *
from astrbot.api import logger
from astrbot.api.message_components import Image, Plain
from astrbot.api.event import MessageEventResult, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import (
    command,
    regex,
    llm_tool,
    permission_type,
    PermissionType,
    event_message_type,
    EventMessageType,
)
from bilibili_api import bangumi
from bilibili_api.bangumi import IndexFilter as IF

from .utils import *
from .renderer import Renderer
from .bili_client import BiliClient
from .listener import DynamicListener
from .data_manager import DataManager
from .constant import category_mapping, VALID_FILTER_TYPES, BV, LOGO_PATH


@register("astrbot_plugin_bilibili", "Soulter", "", "", "")
class Main(Star):
    def __init__(self, context: Context, config: dict) -> None:
        super().__init__(context)
        self.cfg = config
        self.context = context

        self.interval_mins = float(self.cfg.get("interval_mins", 20))
        self.rai = self.cfg.get("rai", True)
        self.node = self.cfg.get("node", False)
        self.enable_parse_video = self.cfg.get("enable_parse_video", False)
        self.enable_parse_miniapp = self.cfg.get("enable_parse_miniapp", False)
        self.enable_bangumi_recommend = self.cfg.get("enable_bangumi_recommend", False)
        self.t2i_url = self.cfg.get("bili_t2i", "")

        self.data_manager = DataManager()
        self.renderer = Renderer(self, self.rai, self.t2i_url)
        self.bili_client = BiliClient(self.cfg.get("sessdata"))
        self.dynamic_listener = DynamicListener(
            context=self.context,
            data_manager=self.data_manager,
            bili_client=self.bili_client,
            renderer=self.renderer,
            interval_mins=self.interval_mins,
            rai=self.rai,
            node=self.node,
        )

        self.dynamic_listener_task = asyncio.create_task(self.dynamic_listener.start())
        
        # 动态注册番剧推荐工具
        if self.enable_bangumi_recommend:
            self._register_bangumi_tool()

    @regex(BV)
    async def get_video_info(self, event: AstrMessageEvent):
        # 检查是否启用视频解析
        if not self.enable_parse_video:
            return
        
        if len(event.message_str) == 12:
            bvid = event.message_str
        else:
            match_ = re.search(BV, event.message_str, re.IGNORECASE)
            if not match_:
                return
            bvid = "BV" + match_.group(1)[2:]

        video_data = await self.bili_client.get_video_info(bvid=bvid)
        if not video_data:
            return await event.send("获取视频信息失败了 (´;ω;`)")
        info = video_data["info"]
        online = video_data["online"]

        render_data = await create_render_data()
        render_data["name"] = "AstrBot"
        render_data["avatar"] = await image_to_base64(LOGO_PATH)
        render_data["title"] = info["title"]
        render_data["text"] = (
            f"UP 主: {info['owner']['name']}<br>"
            f"播放量: {info['stat']['view']}<br>"
            f"点赞: {info['stat']['like']}<br>"
            f"投币: {info['stat']['coin']}<br>"
            f"总共 {online['total']} 人正在观看"
        )
        render_data["image_urls"] = [info["pic"]]

        img_path = await self.renderer.render_dynamic(render_data)
        if img_path:
            await event.send(MessageChain().file_image(img_path))
        else:
            msg = "渲染图片失败了 (´;ω;`)"
            text = "\n".join(filter(None, render_data.get("text", "").split("<br>")))
            await event.send(
                MessageChain().message(msg).message(text).url_image(info["pic"])
            )

    @command("订阅动态", alias={"bili_sub"})
    async def dynamic_sub(self, event: AstrMessageEvent, uid: str, input: GreedyStr):
        args = input.strip().split(" ") if input.strip() else []

        filter_types: List[str] = []
        filter_regex: List[str] = []
        for arg in args:
            if arg in VALID_FILTER_TYPES:
                filter_types.append(arg)
            else:
                filter_regex.append(arg)

        sub_user = event.unified_msg_origin
        if not uid.isdigit():
            return MessageEventResult().message("UID 格式错误")

        # 检查是否已经存在该订阅
        if await self.data_manager.update_subscription(
            sub_user, int(uid), filter_types, filter_regex
        ):
            # 如果已存在，更新其过滤条件
            return MessageEventResult().message("该动态已订阅，已更新过滤条件。")
        # 以下为新增订阅

        usr_info, msg = await self.bili_client.get_user_info(int(uid))
        if not usr_info:
            return MessageEventResult().message(msg)

        mid = usr_info["mid"]
        name = usr_info["name"]
        sex = usr_info["sex"]
        avatar = usr_info["face"]

        # 获取最新一条动态 (用于初始化 last_id)
        try:
            # 构造新的订阅数据结构
            _sub_data = {
                "uid": int(uid),
                "last": "",
                "is_live": False,
                "filter_types": filter_types,
                "filter_regex": filter_regex,
            }
            dyn = await self.bili_client.get_latest_dynamics(int(uid))
            _, dyn_id = await self.dynamic_listener._parse_and_filter_dynamics(
                dyn, _sub_data
            )
            _sub_data["last"] = dyn_id  # 更新 last id
        except Exception as e:
            logger.error(f"获取 {name} 初始动态失败: {e}")

        # 保存配置
        await self.data_manager.add_subscription(sub_user, _sub_data)

        filter_desc = ""
        if filter_types:
            filter_desc += f"<br>过滤类型: {', '.join(filter_types)}"
        if filter_regex:
            filter_desc += f"<br>过滤正则: {filter_regex}"

        render_data = await create_render_data()
        render_data["name"] = "AstrBot"
        render_data["avatar"] = await image_to_base64(LOGO_PATH)
        render_data["text"] = (
            f"📣 订阅成功！<br>"
            f"UP 主: {name} | 性别: {sex}"
            f"{filter_desc}"  # 显示过滤信息
        )
        render_data["image_urls"] = [avatar]
        render_data["url"] = f"https://space.bilibili.com/{mid}"
        render_data["qrcode"] = await create_qrcode(render_data["url"])
        if self.rai:
            img_path = await self.renderer.render_dynamic(render_data)
            if img_path:
                await event.send(
                    MessageChain().file_image(img_path).message(render_data["url"])
                )
            else:
                msg = "渲染图片失败了 (´;ω;`)"
                text = "\n".join(
                    filter(None, render_data.get("text", "").split("<br>"))
                )
                await event.send(
                    MessageChain().message(msg).message(text).url_image(avatar)
                )
        else:
            chain = [
                Plain(render_data["text"]),
                Image.fromURL(avatar),
            ]
            return MessageEventResult(chain=chain, use_t2i_=False)

    @command("订阅列表", alias={"bili_sub_list"})
    async def sub_list(self, event: AstrMessageEvent):
        """查看 bilibili 动态监控列表"""
        sub_user = event.unified_msg_origin
        ret = """订阅列表：\n"""
        subs = self.data_manager.get_subscriptions_by_user(sub_user)

        if not subs:
            return MessageEventResult().message("无订阅")
        else:
            for idx, uid_sub_data in enumerate(subs):
                uid = uid_sub_data["uid"]
                info, _ = await self.bili_client.get_user_info(int(uid))
                if not info:
                    ret += f"{idx + 1}. {uid} - 无法获取 UP 主信息\n"
                else:
                    name = info["name"]
                    ret += f"{idx + 1}. {uid} - {name}\n"
            return MessageEventResult().message(ret)

    @command("订阅删除", alias={"bili_sub_del"})
    async def sub_del(self, event: AstrMessageEvent, uid: str):
        """删除 bilibili 动态监控"""
        sub_user = event.unified_msg_origin
        if not uid or not uid.isdigit():
            return MessageEventResult().message("参数错误，请提供正确的UID。")

        uid2del = int(uid)

        if await self.data_manager.remove_subscription(sub_user, uid2del):
            return MessageEventResult().message("删除成功")
        else:
            return MessageEventResult().message("未找到指定的订阅")

    def _register_bangumi_tool(self):
        """动态注册番剧推荐工具"""
        @llm_tool("get_bangumi")
        async def get_bangumi(
            self,
            event: AstrMessageEvent,
            style: str = "ALL",
            season: str = "ALL",
            start_year: int = None,
            end_year: int = None,
        ):
            """当用户希望推荐番剧时调用。根据用户的描述获取前 5 条推荐的动漫番剧。

            Args:
                style(string): 番剧的风格。默认为全部。可选值有：原创, 漫画改, 小说改, 游戏改, 特摄, 布袋戏, 热血, 穿越, 奇幻, 战斗, 搞笑, 日常, 科幻, 萌系, 治愈, 校园, 儿童, 泡面, 恋爱, 少女, 魔法, 冒险, 历史, 架空, 机战, 神魔, 声控, 运动, 励志, 音乐, 推理, 社团, 智斗, 催泪, 美食, 偶像, 乙女, 职场
                season(string): 番剧的季度。默认为全部。可选值有：WINTER, SPRING, SUMMER, AUTUMN。其也分别代表一月番、四月番、七月番、十月番
                start_year(number): 起始年份。默认为空，即不限制年份。
                end_year(number): 结束年份。默认为空，即不限制年份。
            """
            if style in category_mapping:
                style = getattr(IF.Style.Anime, category_mapping[style], IF.Style.Anime.ALL)
            else:
                style = IF.Style.Anime.ALL

            if season in ["WINTER", "SPRING", "SUMMER", "AUTUMN"]:
                season = getattr(IF.Season, season, IF.Season.ALL)
            else:
                season = IF.Season.ALL

            filters = bangumi.IndexFilterMeta.Anime(
                area=IF.Area.JAPAN,
                year=IF.make_time_filter(start=start_year, end=end_year, include_end=True),
                season=season,
                style=style,
            )
            index = await bangumi.get_index_info(
                filters=filters, order=IF.Order.SCORE, sort=IF.Sort.DESC, pn=1, ps=5
            )

            result = "推荐的番剧:\n"
            for item in index["list"]:
                result += f"标题: {item['title']}\n"
                result += f"副标题: {item['subTitle']}\n"
                result += f"评分: {item['score']}\n"
                result += f"集数: {item['index_show']}\n"
                result += f"链接: {item['link']}\n"
                result += "\n"
            result += "请分点，贴心地回答。不要输出 markdown 格式。"
            return result
        
        # 将方法绑定到实例
        self.get_bangumi = get_bangumi.__get__(self, self.__class__)

    @permission_type(PermissionType.ADMIN)
    @command("全局删除", alias={"bili_global_del"})
    async def global_sub_del(self, event: AstrMessageEvent, sid: str = None):
        """管理员指令。通过 SID 删除某一个群聊或者私聊的所有订阅。使用 /sid 查看当前会话的 SID。"""
        if not sid:
            return MessageEventResult().message(
                "通过 SID 删除某一个群聊或者私聊的所有订阅。使用 /sid 指令查看当前会话的 SID。"
            )

        msg = await self.data_manager.remove_all_for_user(sid)
        return MessageEventResult().message(msg)

    @permission_type(PermissionType.ADMIN)
    @command("全局订阅", alias={"bili_global_sub"})
    async def global_sub_add(
        self, event: AstrMessageEvent, sid: str, uid: str, input: GreedyStr
    ):
        """管理员指令。通过 UID 添加某一个用户的所有订阅。"""
        if not sid or not uid.isdigit():
            return MessageEventResult().message(
                "请提供正确的SID与UID。使用 /sid 指令查看当前会话的 SID"
            )
        args = input.strip().split(" ") if input.strip() else []
        filter_types: List[str] = []
        filter_regex: List[str] = []
        for arg in args:
            if arg in VALID_FILTER_TYPES:
                filter_types.append(arg)
            else:
                filter_regex.append(arg)

        if await self.data_manager.update_subscription(
            sid, int(uid), filter_types, filter_regex
        ):
            return MessageEventResult().message("该动态已订阅，已更新过滤条件")

        usr_info, msg = await self.bili_client.get_user_info(int(uid))
        if not usr_info:
            return MessageEventResult().message(msg)
        try:
            _sub_data = {
                "uid": uid,
                "last": "",
                "is_live": False,
                "filter_types": filter_types,
                "filter_regex": filter_regex,
            }
            dyn = await self.bili_client.get_latest_dynamics(int(uid))
            _, dyn_id = await self.dynamic_listener._parse_and_filter_dynamics(
                dyn, _sub_data
            )
            _sub_data["last"] = dyn_id
        except Exception as e:
            logger.error(f"获取 {usr_info['name']} 初始动态失败: {e}")

        await self.data_manager.add_subscription(sid, _sub_data)
        return MessageEventResult().message(f"为添加{sid}订阅{uid}成功")

    @permission_type(PermissionType.ADMIN)
    @command("全局列表", alias={"bili_global_list"})
    async def global_list(self, event: AstrMessageEvent):
        """管理员指令。查看所有订阅者"""
        ret = "订阅会话列表：\n"
        all_subs = self.data_manager.get_all_subscriptions()
        if not all_subs:
            return MessageEventResult().message("没有任何会话订阅过。")

        for sub_user in all_subs:
            ret += f"- {sub_user}\n"
            for sub in all_subs[sub_user]:
                uid = sub.get("uid")
                ret += f"  - {uid}\n"
        return MessageEventResult().message(ret)

    @event_message_type(EventMessageType.ALL)
    async def parse_miniapp(self, event: AstrMessageEvent):
        # 检查是否启用小程序解析
        if not self.enable_parse_miniapp:
            return
            
        for msg_element in event.message_obj.message:
            if (
                hasattr(msg_element, "type")
                and msg_element.type == "Json"
                and hasattr(msg_element, "data")
            ):
                json_string = msg_element.data

                try:
                    parsed_data = json.loads(json_string)
                    meta = parsed_data.get("meta", {})
                    detail_1 = meta.get("detail_1", {})
                    title = detail_1.get("title")
                    qqdocurl = detail_1.get("qqdocurl")
                    desc = detail_1.get("desc")

                    if title == "哔哩哔哩" and qqdocurl:
                        if "https://b23.tv" in qqdocurl:
                            qqdocurl = await self.bili_client.b23_to_bv(qqdocurl)
                        ret = f"视频: {desc}\n链接: {qqdocurl}"
                        await event.send(MessageChain().message(ret))
                    news = meta.get("news", {})
                    tag = news.get("tag", "")
                    jumpurl = news.get("jumpUrl", "")
                    title = news.get("title", "")
                    if tag == "哔哩哔哩" and jumpurl:
                        if "https://b23.tv" in jumpurl:
                            jumpurl = await self.bili_client.b23_to_bv(jumpurl)
                        ret = f"视频: {title}\n链接: {jumpurl}"
                        await event.send(MessageChain().message(ret))
                except json.JSONDecodeError:
                    logger.error(f"Failed to decode JSON string: {json_string}")
                except Exception as e:
                    logger.error(f"An error occurred during JSON processing: {e}")

    @command("订阅测试", alias={"bili_sub_test"})
    async def sub_test(self, event: AstrMessageEvent, uid: str):
        """测试订阅功能。仅测试获取动态与渲染图片功能，不保存订阅信息。"""
        sub_user = event.unified_msg_origin
        dyn = await self.bili_client.get_latest_dynamics(int(uid))
        if dyn:
            render_data, _ = await self.dynamic_listener._parse_and_filter_dynamics(
                dyn, {"uid": uid, "filter_types": [], "filter_regex": [], "last": ""}
            )
            await self.dynamic_listener._handle_new_dynamic(sub_user, render_data)

    @command("动态调试", alias={"bili_debug"})
    async def debug_dynamics(self, event: AstrMessageEvent, uid: str):
        """调试命令：查看某个 UP 主最新动态的原始数据结构"""
        dyn = await self.bili_client.get_latest_dynamics(int(uid))
        if not dyn:
            return MessageEventResult().message("获取动态失败")
        
        items = dyn.get("items", [])
        if not items:
            return MessageEventResult().message("该 UP 主没有动态")
        
        # 只看第一条动态
        item = items[0]
        result = f"动态 ID: {item.get('id_str')}\n"
        result += f"动态类型: {item.get('type')}\n"
        result += f"包含的模块: {', '.join(item.get('modules', {}).keys())}\n"
        
        if "module_tag" in item.get("modules", {}):
            result += f"\nmodule_tag:\n{item['modules']['module_tag']}\n"
        
        if "display" in item.get("modules", {}):
            result += f"\ndisplay:\n{item['modules']['display']}\n"
        
        # 获取动态内容摘要
        try:
            if item.get("type") in ("DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"):
                major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})
                if "opus" in major:
                    summary = major["opus"]["summary"]["text"][:100]
                    result += f"\n内容摘要: {summary}...\n"
        except:
            pass
        
        return MessageEventResult().message(result)

    async def terminate(self):
        if self.dynamic_listener_task and not self.dynamic_listener_task.done():
            self.dynamic_listener_task.cancel()
            try:
                await self.dynamic_listener_task
            except asyncio.CancelledError:
                logger.info(
                    "bilibili dynamic_listener task was successfully cancelled during terminate."
                )
            except Exception as e:
                logger.error(
                    f"Error awaiting cancellation of dynamic_listener task: {e}"
                )
