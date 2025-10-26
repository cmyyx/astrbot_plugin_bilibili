import aiohttp
from astrbot.api import logger
from typing import Optional, Dict, Any, Tuple
from bilibili_api import user, Credential, video


class BiliClient:
    """
    负责所有与 Bilibili API 的交互。
    """

    def __init__(self, sessdata: Optional[str] = None):
        """
        初始化 Bilibili API 客户端。
        """
        self.credential = None
        if sessdata:
            self.credential = Credential(sessdata=sessdata)
        else:
            logger.warning("未提供 SESSDATA，部分需要登录的API可能无法使用。")

    async def get_user(self, uid: int) -> user.User:
        """
        根据UID获取一个 User 对象。
        """
        return user.User(uid=uid, credential=self.credential)

    async def get_video_info(self, bvid: str) -> Optional[Dict[str, Any]]:
        """
        获取视频的详细信息和在线观看人数。
        """
        try:
            v = video.Video(bvid=bvid)
            info = await v.get_info()
            online = await v.get_online()
            return {"info": info, "online": online}
        except Exception as e:
            logger.error(f"获取视频信息失败 (BVID: {bvid}): {e}")
            return None

    async def get_latest_dynamics(self, uid: int) -> Optional[Dict[str, Any]]:
        """
        获取用户的最新动态。
        """
        try:
            u = await self.get_user(uid)
            return await u.get_dynamics_new()
        except Exception as e:
            logger.error(f"获取用户动态失败 (UID: {uid}): {e}")
            return None

    async def get_dynamic_detail(self, dynamic_id: str) -> Optional[Dict[str, Any]]:
        """
        通过动态 ID 获取单条动态的详细信息。
        """
        try:
            from bilibili_api import dynamic
            dyn = dynamic.Dynamic(dynamic_id=dynamic_id, credential=self.credential)
            return await dyn.get_info()
        except Exception as e:
            logger.error(f"获取动态详情失败 (Dynamic ID: {dynamic_id}): {e}")
            return None

    async def get_live_info(self, uid: int) -> Optional[Dict[str, Any]]:
        """
        获取用户的直播间信息。
        """
        try:
            u = await self.get_user(uid)
            return await u.get_live_info()
        except Exception as e:
            logger.error(f"获取直播间信息失败 (UID: {uid}): {e}")
            return None

    async def get_user_info(self, uid: int) -> Optional[Tuple[Dict[str, Any], str]]:
        """
        获取用户的基本信息。
        """
        try:
            u = await self.get_user(uid)
            info = await u.get_user_info()
            return info, ""
        except Exception as e:
            if "code" in e.args[0] and e.args[0]["code"] == -404:
                logger.warning(f"无法找到用户 (UID: {uid})")
                return None, "啥都木有 (´;ω;`)"
            else:
                logger.error(f"获取用户信息失败 (UID: {uid}): {e}")
                return None, f"获取 UP 主信息失败: {str(e)}"

    async def b23_to_bv(self, url: str) -> Optional[str]:
        """
        b23短链转换为原始链接
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    url=url, headers=headers, allow_redirects=False, timeout=10
                ) as response:
                    if 300 <= response.status < 400:
                        location_url = response.headers.get("Location")
                        if location_url:
                            base_url = location_url.split("?", 1)[0]
                            return base_url
            except Exception as e:
                logger.error(f"解析b23链接失败 (URL: {url}): {e}")
                return url
