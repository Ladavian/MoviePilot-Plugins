import datetime
import re
import xml.dom.minidom
from threading import Event
from typing import Optional, Tuple, List, Dict, Any, TypedDict
import time
import random
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from enum import Enum

from app import schemas
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils
from app.modules.douban.apiv2 import DoubanApi


class Status(Enum):
    UNRECOGNIZED = "未识别"
    UNCATEGORIZED = "已识别未分类"
    YEAR_NOT_MATCH = "年份不符合"
    RATING_NOT_MATCH = "评分不符合"
    MEDIA_EXISTS = "媒体库已存在"
    SUBSCRIPTION_EXISTS = "订阅已存在"
    SUBSCRIPTION_ADDED = "已添加订阅"


class HistoryDataType(Enum):
    STATISTICS = "历史处理统计"
    RECOGNIZED = "已识别历史"
    UNRECOGNIZED = "未识别历史"
    ALL = "所有历史"
    LATEST = "最新12条历史"


class Icons(Enum):
    RECOGNIZED = "icon_recognized"
    STATISTICS = "icon_statistics"
    UNRECOGNIZED = "icon_unrecognized"
    RSS = "icon_rss"


class HistoryPayload(TypedDict):
    title: str
    type: str
    year: str
    poster: Optional[str]
    overview: str
    tmdbid: str
    doubanid: str
    unique: str
    time: str
    time_full: str
    vote: float
    status: str


class RssInfo(TypedDict):
    title: str
    link: str
    mtype: str
    doubanid: str | None
    year: str | None


class DoubanRankPlus(_PluginBase):
    # 插件名称
    plugin_name = "豆瓣榜单Plus"
    # 插件描述
    plugin_desc = "自动订阅豆瓣热门榜单增强版"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/boeto/MoviePilot-Plugins/main/icons/DouBanRankPlus.png"
    # 插件版本
    plugin_version = "0.0.15"
    # 插件作者
    plugin_author = "boeto"
    # 作者主页
    author_url = "https://github.com/boeto/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "doubanrankplus_"
    # 加载顺序
    plugin_order = 7
    # 可使用的用户级别
    auth_level = 2

    # 退出事件
    _event = Event()

    # 私有属性
    downloadchain: DownloadChain = None
    subscribechain: SubscribeChain = None
    mediachain: MediaChain = None
    doubanapi: DoubanApi = None

    _scheduler = None
    _douban_address = {
        "movie-ustop": "https://rsshub.app/douban/movie/ustop",
        "movie-weekly": "https://rsshub.app/douban/movie/weekly",
        "movie-real-time": "https://rsshub.app/douban/movie/weekly/movie_real_time_hotest",
        "show-domestic": "https://rsshub.app/douban/movie/weekly/show_domestic",
        "movie-hot-gaia": "https://rsshub.app/douban/movie/weekly/movie_hot_gaia",
        "tv-hot": "https://rsshub.app/douban/movie/weekly/tv_hot",
        "movie-top250": "https://rsshub.app/douban/movie/weekly/movie_top250",
        "movie-top250-full": "https://rsshub.app/douban/list/movie_top250",
    }

    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _rss_addrs: List[str] = []
    _ranks: List[str] = []
    _vote: float = 0.0
    _clear: bool = False
    _clearflag: bool = False
    _clear_unrecognized: bool = False
    _clearflag_unrecognized: bool = False
    _proxy: bool = False
    _is_seasons_all: bool = True
    _release_year: int = 0
    _min_sleep_time: int = 3
    _max_sleep_time: int = 10
    _history_type: str = HistoryDataType.LATEST.value
    _is_exit_ip_rate_limit: bool = False

    def init_plugin(self, config: dict[str, Any] | None = None):
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.mediachain = MediaChain()
        self.doubanapi = DoubanApi()

        if config:
            self._enabled = config.get("enabled", False)
            self._proxy = config.get("proxy", False)
            self._onlyonce = config.get("onlyonce", False)
            self._is_seasons_all = config.get("is_seasons_all", True)

            self._cron = (
                config.get("cron", "").strip() if config.get("cron", "").strip() else ""
            )

            self._release_year = (
                int(config.get("release_year", "").strip())
                if config.get("release_year", "").strip()
                else 0
            )

            self._vote = (
                float(str(config.get("vote", "")).strip())
                if str(config.get("vote", "")).strip()
                else 0.0
            )

            __sleep_time = config.get("sleep_time", "3,10").strip()
            __sleep_time_list = re.split("[,，]", __sleep_time)

            self._min_sleep_time, self._max_sleep_time = 3, 10  # default values

            if len(__sleep_time_list) == 2:
                __min_sleep_time, __max_sleep_time = map(int, __sleep_time_list)
                if __max_sleep_time >= __min_sleep_time:
                    self._min_sleep_time = __min_sleep_time
                    self._max_sleep_time = __max_sleep_time
                else:
                    logger.warn("最大休眠时间小于最小休眠时间,使用默认值")
            else:
                logger.warn("休眠时间配置格式不正确,使用默认值")

            rss_addrs = config.get("rss_addrs")
            if rss_addrs and isinstance(rss_addrs, str):
                self._rss_addrs = rss_addrs.split("\n")
            else:
                self._rss_addrs = []

            self._ranks = config.get("ranks", [])
            self._clear = config.get("clear", False)
            self._clear_unrecognized = config.get("clear_unrecognized", False)
            self._history_type = config.get(
                "history_type", HistoryDataType.LATEST.value
            )
            self._is_exit_ip_rate_limit = config.get("is_exit_ip_rate_limit", False)

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("豆瓣榜单Plus服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.__refresh_rss,
                    trigger="date",
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3),
                )

                if self._scheduler.get_jobs():
                    # 启动服务
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._onlyonce or self._clear:
                # 记录缓存清理标志
                self._clearflag = self._clear
                # 关闭清理缓存
                self._clear = False

            if self._onlyonce or self._clear_unrecognized:
                # 记录未识别缓存清理标志
                self._clearflag_unrecognized = self._clear_unrecognized
                # 关闭未识别清理缓存
                self._clear_unrecognized = False

            if self._onlyonce or self._clear or self._clear_unrecognized:
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除豆瓣榜单Plus历史记录",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [
                {
                    "id": "DoubanRankPlus",
                    "name": "豆瓣榜单Plus服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.__refresh_rss,
                    "kwargs": {},
                }
            ]
        elif self._enabled:
            return [
                {
                    "id": "DoubanRankPlus",
                    "name": "豆瓣榜单Plus服务",
                    "trigger": CronTrigger.from_crontab("0 8 * * *"),
                    "func": self.__refresh_rss,
                    "kwargs": {},
                }
            ]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "proxy",
                                            "label": "使用代理服务器",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "is_seasons_all",
                                            "label": "订阅剧集全季度",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "is_exit_ip_rate_limit",
                                            "label": "未能从豆瓣获取数据时结束",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "5位cron表达式，留空自动",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "sleep_time",
                                            "label": "随机休眠时间范围",
                                            "placeholder": "默认: 3,10。减少豆瓣访问频率。格式：最小秒数,最大秒数。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "vote",
                                            "label": "评分",
                                            "placeholder": "评分大于等于该值才订阅",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "release_year",
                                            "label": "上映年份",
                                            "placeholder": "年份大于等于该值才订阅",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VCol",
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "history_type",
                                            "label": "数据面板历史显示",
                                            "items": [
                                                {
                                                    "title": f"{HistoryDataType.LATEST.value}",
                                                    "value": f"{HistoryDataType.LATEST.value}",
                                                },
                                                {
                                                    "title": f"{HistoryDataType.RECOGNIZED.value}",
                                                    "value": f"{HistoryDataType.RECOGNIZED.value}",
                                                },
                                                {
                                                    "title": f"{HistoryDataType.UNRECOGNIZED.value}",
                                                    "value": f"{HistoryDataType.UNRECOGNIZED.value}",
                                                },
                                                {
                                                    "title": f"{HistoryDataType.ALL.value}",
                                                    "value": f"{HistoryDataType.ALL.value}",
                                                },
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "chips": True,
                                            "multiple": True,
                                            "model": "ranks",
                                            "label": "热门榜单",
                                            "items": [
                                                {
                                                    "title": "电影北美票房榜",
                                                    "value": "movie-ustop",
                                                },
                                                {
                                                    "title": "一周口碑电影榜",
                                                    "value": "movie-weekly",
                                                },
                                                {
                                                    "title": "实时热门电影",
                                                    "value": "movie-real-time",
                                                },
                                                {
                                                    "title": "热门综艺",
                                                    "value": "show-domestic",
                                                },
                                                {
                                                    "title": "热门电影",
                                                    "value": "movie-hot-gaia",
                                                },
                                                {
                                                    "title": "热门电视剧",
                                                    "value": "tv-hot",
                                                },
                                                {
                                                    "title": "电影TOP10",
                                                    "value": "movie-top250",
                                                },
                                                {
                                                    "title": "电影TOP250",
                                                    "value": "movie-top250-full",
                                                },
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "rss_addrs",
                                            "label": "自定义榜单地址",
                                            "placeholder": "每行一个地址。地址后加分号 `;`，自定义地址的下载路径。分号后用#按类型分割下载路径/电影#/电视剧#/动漫，\nhttps://rsshub.app/douban/movie/ustop\nhttps://rsshub.app/douban/movie/ustop;/download_to_path\nhttps://rsshub.app/douban/doulist/44852852;/movie_path#/tv_path#/anime_path",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear",
                                            "label": "清理历史记录",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear_unrecognized",
                                            "label": "清理未识别历史记录",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "cron": "",
            "proxy": False,
            "onlyonce": False,
            "vote": 0.0,
            "ranks": [],
            "rss_addrs": [],
            "clear": False,
            "clear_unrecognized": False,
            "release_year": "0",
            "sleep_time": "3,10",
            "is_seasons_all": True,
            "history_type": HistoryDataType.LATEST.value,
            "is_exit_ip_rate_limit": False,
        }

    @staticmethod
    def __get_svg_content(color: str, ds: List[str]):
        def __get_path_content(fill: str, d: str) -> dict[str, Any]:
            return {
                "component": "path",
                "props": {"fill": fill, "d": d},
            }

        path_content = [__get_path_content(color, d) for d in ds]
        component = {
            "component": "svg",
            "props": {
                "class": "icon",
                "viewBox": "0 0 1024 1024",
                "width": "40",
                "height": "40",
            },
            "content": path_content,
        }
        return component

    @staticmethod
    def __get_icon_content():
        color = "#8a8a8a"
        icon_content = {
            Icons.RECOGNIZED: DoubanRankPlus.__get_svg_content(
                color,
                [
                    "M512 417.792c-53.248 0-94.208 40.96-94.208 94.208 0 53.248 40.96 94.208 94.208 94.208 53.248 0 94.208-40.96 94.208-94.208 0-53.248-40.96-94.208-94.208-94.208z",
                    "M512 229.376C245.76 229.376 36.864 475.136 28.672 487.424c-12.288 16.384-12.288 36.864 0 53.248 8.192 12.288 217.088 258.048 483.328 258.048 266.24 0 475.136-245.76 483.328-258.048 12.288-16.384 12.288-36.864 0-53.248-8.192-12.288-217.088-258.048-483.328-258.048z m0 479.232c-106.496 0-196.608-90.112-196.608-196.608 0-110.592 90.112-196.608 196.608-196.608 110.592 0 196.608 90.112 196.608 196.608 0 110.592-86.016 196.608-196.608 196.608zM61.44 741.376c-24.576 0-40.96 16.384-40.96 40.96v180.224c0 24.576 16.384 40.96 40.96 40.96h180.224c24.576 0 40.96-16.384 40.96-40.96s-16.384-40.96-40.96-40.96H102.4v-139.264c0-24.576-16.384-40.96-40.96-40.96zM61.44 282.624c24.576 0 40.96-16.384 40.96-40.96V102.4H245.76c24.576 0 40.96-16.384 40.96-40.96s-16.384-40.96-40.96-40.96H61.44c-24.576 0-40.96 16.384-40.96 40.96V245.76c0 20.48 16.384 36.864 40.96 36.864zM782.336 102.4h139.264v139.264c0 24.576 16.384 40.96 40.96 40.96s40.96-16.384 40.96-40.96V61.44c0-24.576-16.384-40.96-40.96-40.96h-180.224c-24.576 0-40.96 16.384-40.96 40.96s16.384 40.96 40.96 40.96zM962.56 741.376c-24.576 0-40.96 16.384-40.96 40.96v143.36h-139.264c-24.576 0-40.96 16.384-40.96 40.96s16.384 40.96 40.96 40.96h180.224c24.576 0 40.96-16.384 40.96-40.96v-184.32c0-24.576-16.384-40.96-40.96-40.96z",
                ],
            ),
            Icons.STATISTICS: DoubanRankPlus.__get_svg_content(
                color,
                [
                    "M471.04 270.336V20.48c-249.856 20.48-450.56 233.472-450.56 491.52 0 274.432 225.28 491.52 491.52 491.52 118.784 0 229.376-40.96 315.392-114.688L655.36 708.608c-40.96 28.672-94.208 45.056-139.264 45.056-135.168 0-245.76-106.496-245.76-245.76 0-114.688 81.92-217.088 200.704-237.568z",
                    "M552.96 20.48v249.856C655.36 286.72 737.28 368.64 753.664 471.04h249.856C983.04 233.472 790.528 40.96 552.96 20.48zM712.704 651.264l176.128 176.128c65.536-77.824 106.496-172.032 114.688-274.432h-249.856c-8.192 36.864-20.48 69.632-40.96 98.304z",
                ],
            ),
            Icons.UNRECOGNIZED: DoubanRankPlus.__get_svg_content(
                color,
                [
                    "M241.664 921.6H102.4v-139.264c0-24.576-16.384-40.96-40.96-40.96s-40.96 16.384-40.96 40.96v180.224c0 24.576 16.384 40.96 40.96 40.96h180.224c24.576 0 40.96-16.384 40.96-40.96s-16.384-40.96-40.96-40.96zM245.76 20.48H61.44c-24.576 0-40.96 16.384-40.96 40.96V245.76c0 24.576 16.384 40.96 40.96 40.96s40.96-16.384 40.96-40.96V102.4H245.76c24.576 0 40.96-16.384 40.96-40.96s-20.48-40.96-40.96-40.96zM962.56 20.48h-180.224c-24.576 0-40.96 16.384-40.96 40.96s16.384 40.96 40.96 40.96h139.264v139.264c0 24.576 16.384 40.96 40.96 40.96s40.96-16.384 40.96-40.96V61.44c0-24.576-16.384-40.96-40.96-40.96zM962.56 741.376c-24.576 0-40.96 16.384-40.96 40.96v143.36h-139.264c-24.576 0-40.96 16.384-40.96 40.96s16.384 40.96 40.96 40.96h180.224c24.576 0 40.96-16.384 40.96-40.96v-184.32c0-24.576-16.384-40.96-40.96-40.96zM696.32 401.408c0-102.4-81.92-184.32-184.32-184.32S327.68 299.008 327.68 401.408c0 57.344 24.576 110.592 69.632 143.36l-36.864 204.8c-4.096 12.288 0 28.672 8.192 36.864 8.192 12.288 20.48 16.384 36.864 16.384h212.992c12.288 0 28.672-4.096 36.864-16.384 8.192-12.288 12.288-24.576 8.192-36.864l-36.864-204.8c45.056-28.672 69.632-81.92 69.632-143.36z"
                ],
            ),
            Icons.RSS: DoubanRankPlus.__get_svg_content(
                color,
                [
                    "M320.16155 831.918c0 70.738-57.344 128.082-128.082 128.082S63.99955 902.656 63.99955 831.918s57.344-128.082 128.082-128.082 128.08 57.346 128.08 128.082z m351.32 94.5c-16.708-309.2-264.37-557.174-573.9-573.9C79.31155 351.53 63.99955 366.21 63.99955 384.506v96.138c0 16.83 12.98 30.944 29.774 32.036 223.664 14.568 402.946 193.404 417.544 417.544 1.094 16.794 15.208 29.774 32.036 29.774h96.138c18.298 0.002 32.978-15.31 31.99-33.58z m288.498 0.576C943.19155 459.354 566.92955 80.89 97.00555 64.02 78.94555 63.372 63.99955 77.962 63.99955 96.032v96.136c0 17.25 13.67 31.29 30.906 31.998 382.358 15.678 689.254 322.632 704.93 704.93 0.706 17.236 14.746 30.906 31.998 30.906h96.136c18.068-0.002 32.658-14.948 32.01-33.008z"
                ],
            ),
        }
        return icon_content

    @staticmethod
    def __get_historys_statistic_content(
        title: str, value: str, icon_name: str
    ) -> dict[str, Any]:
        icon_content = DoubanRankPlus.__get_icon_content().get(icon_name, "")
        total_elements = {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [
                {
                    "component": "VCard",
                    "props": {
                        "variant": "tonal",
                    },
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {
                                "class": "d-flex align-center",
                            },
                            "content": [
                                icon_content,
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "ml-2",
                                    },
                                    "content": [
                                        {
                                            "component": "span",
                                            "props": {"class": "text-caption"},
                                            "text": f"{title}",
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "d-flex align-center flex-wrap"
                                            },
                                            "content": [
                                                {
                                                    "component": "span",
                                                    "props": {"class": "text-h6"},
                                                    "text": f"{value}",
                                                }
                                            ],
                                        },
                                    ],
                                },
                            ],
                        }
                    ],
                },
            ],
        }
        return total_elements

    def __get_historys_statistics_content(
        self, historys_total, historys_recognized_total, historys_unrecognized_total
    ):
        addr_list = self._rss_addrs + [
            self._douban_address.get(rank) for rank in self._ranks
        ]

        # 数据统计
        data_statistics = [
            {
                "title": "历史总计数量",
                "value": historys_total,
                "icon_name": Icons.STATISTICS,
            },
            {
                "title": "已识别数量",
                "value": historys_recognized_total,
                "icon_name": Icons.RECOGNIZED,
            },
            {
                "title": "未识别数量",
                "value": historys_unrecognized_total,
                "icon_name": Icons.UNRECOGNIZED,
            },
            {
                "title": "榜单数量",
                "value": len(addr_list),
                "icon_name": Icons.RSS,
            },
        ]

        content = list(
            map(
                lambda s: DoubanRankPlus.__get_historys_statistic_content(
                    title=s["title"],
                    value=s["value"],
                    icon_name=s["icon_name"],
                ),
                data_statistics,
            )
        )

        component = {"component": "VRow", "content": content}
        return component

    def __get_history_post_content(self, history: HistoryPayload):
        title = history.get("title", "")
        if len(title) > 8:
            title = title[:8] + "..."
        title = title.replace(" ", "")

        year = history.get("year")
        vote = history.get("vote")
        poster = history.get("poster")
        time_str = history.get("time")
        mtype = history.get("type")
        doubanid = history.get("doubanid")
        tmdbid = history.get("tmdbid")

        status = history.get("status")
        unique = history.get("unique")

        if (
            tmdbid
            and tmdbid != "0"
            and (mtype == MediaType.MOVIE.value or mtype == MediaType.TV.value)
        ):
            type_str = "movie" if mtype == MediaType.MOVIE.value else "tv"
            href = f"https://www.themoviedb.org/{type_str}/{tmdbid}"
        elif doubanid and doubanid != "0":
            href = f"https://movie.douban.com/subject/{doubanid}"
        else:
            href = "#"

        component = {
            "component": "VCard",
            "props": {
                "variant": "tonal",
            },
            "content": [
                {
                    "component": "VDialogCloseBtn",
                    "props": {
                        "innerClass": "absolute -top-4 right-0 scale-50 opacity-50",
                    },
                    "events": {
                        "click": {
                            "api": "plugin/DoubanRankPlus/delete_history",
                            "method": "get",
                            "params": {
                                "key": f"{unique}",
                                "apikey": settings.API_TOKEN,
                            },
                        }
                    },
                },
                {
                    "component": "div",
                    "props": {
                        "class": "d-flex justify-space-start flex-nowrap flex-row",
                    },
                    "content": [
                        {
                            "component": "div",
                            "content": [
                                {
                                    "component": "VImg",
                                    "props": {
                                        "src": poster,
                                        "height": 150,
                                        "width": 100,
                                        "aspect-ratio": "2/3",
                                        "class": "object-cover shadow ring-gray-500",
                                        "cover": True,
                                        "transition": True,
                                        "lazy-src": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGQAAACWCAQAAACCseXNAAAAkklEQVR42u3PAREAAAQEMJ9cFFUVkMBtDZbpeiEiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIpcFcbGoK4SMl3wAAAAASUVORK5CYII=",  # 添加懒加载
                                    },
                                }
                            ],
                        },
                        {
                            "component": "div",
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {
                                        "class": "py-1 pl-2 pr-4 text-lg whitespace-nowrap"
                                    },
                                    "content": [
                                        {
                                            "component": "a",
                                            "props": {
                                                "href": f"{href}",
                                                "target": "_blank",
                                            },
                                            "text": title,
                                        }
                                    ],
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"类型: {mtype}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"年份: {year}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"评分: {vote}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"时间: {time_str}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"状态: {status}",
                                },
                            ],
                        },
                    ],
                },
            ],
        }

        return component

    def __get_historys_posts_content(self, historys: List[HistoryPayload] | None):
        posts_content = []
        if not historys:
            posts_content = [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {
                        "class": "text-start",
                    },
                }
            ]
        else:
            for history in historys:
                posts_content.append(self.__get_history_post_content(history))

        component = {
            "component": "div",
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {"class": "pt-6 pb-2 px-0 text-base whitespace-nowrap"},
                    "content": [
                        {
                            "component": "span",
                            "text": f"{self._history_type}",
                        }
                    ],
                },
                {
                    "component": "div",
                    "props": {
                        "class": "grid gap-3 grid-info-card p-4",
                    },
                    "content": posts_content,
                },
            ],
        }

        return component

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """

        # 查询历史记录
        historys = self.get_data("history")
        if not historys:
            return [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {
                        "class": "text-center",
                    },
                }
            ]

        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get("time_full"), reverse=True)

        history_recognized = []
        history_unrecognized = []

        for history in historys:
            if history.get("status") != Status.UNRECOGNIZED.value:
                history_recognized.append(history)
            else:
                history_unrecognized.append(history)

        history_recognized = sorted(
            history_recognized, key=lambda x: x.get("time_full"), reverse=True
        )
        history_unrecognized = sorted(
            history_unrecognized, key=lambda x: x.get("time_full"), reverse=True
        )

        historys_total = len(historys)
        historys_recognized_total = len(history_recognized)
        historys_unrecognized_total = len(history_unrecognized)

        historys_in_type: list[HistoryPayload] | None = None
        if self._history_type == HistoryDataType.LATEST.value:
            historys_in_type = historys[:12]
        elif self._history_type == HistoryDataType.RECOGNIZED.value:
            historys_in_type = history_recognized
        elif self._history_type == HistoryDataType.UNRECOGNIZED.value:
            historys_in_type = history_unrecognized
        elif self._history_type == HistoryDataType.ALL.value:
            historys_in_type = historys

        historys_posts_content = self.__get_historys_posts_content(historys_in_type)
        historys_statistics_content = self.__get_historys_statistics_content(
            historys_total, historys_recognized_total, historys_unrecognized_total
        )

        # 拼装页面
        return [
            {
                "component": "div",
                "content": [
                    historys_statistics_content,
                    historys_posts_content,
                ],
            }
        ]

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))

    def delete_history(self, key: str, apikey: str):
        """
        删除同步历史记录
        """
        logger.debug(f"删除同步历史记录:::{key}")
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        # 历史记录
        historys = self.get_data("history")
        if not historys:
            return schemas.Response(success=False, message="未找到历史记录")
        # 删除指定记录
        historys = [h for h in historys if h.get("unique") != key]
        self.save_data("history", historys)
        return schemas.Response(success=True, message="删除成功")

    def __update_config(self):
        """
        更新配置
        """
        __config = {
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "vote": self._vote,
            "ranks": self._ranks,
            "rss_addrs": "\n".join(map(str, self._rss_addrs)),
            "clear": self._clear,
            "clear_unrecognized": self._clear_unrecognized,
            "is_seasons_all": self._is_seasons_all,
            "release_year": str(self._release_year),
            "sleep_time": f"{self._min_sleep_time},{self._max_sleep_time}",
            "history_type": self._history_type,
            "is_exit_ip_rate_limit": self._is_exit_ip_rate_limit,
        }
        logger.debug(f"更新配置 {__config}")
        self.update_config(__config)

    def __refresh_rss(self):
        """
        刷新RSS
        """
        logger.info("开始刷新豆瓣榜单Plus ...")
        addr_list = self._rss_addrs + [
            self._douban_address.get(rank) for rank in self._ranks
        ]
        if not addr_list:
            logger.info("未设置榜单RSS地址")
            return
        else:
            logger.info(f"共 {len(addr_list)} 个榜单RSS地址需要刷新")

        # 读取历史记录
        if self._clearflag:
            history = []
            self.save_data("history", history)
            # 历史只清理一次
            self._clearflag = False
            logger.info(f"已清理所有 {self.plugin_name} 的历史记录")
        else:
            history = self.get_data("history") or []
            if history and self._clearflag_unrecognized:
                original_length = len(history)
                history = [
                    h for h in history if h.get("status") != Status.UNRECOGNIZED.value
                ]
                deleted_count = original_length - len(history)
                self.save_data("history", history)
                # 未识别历史只清理一次
                self._clearflag_unrecognized = False
                logger.info(
                    f"已清理 {deleted_count} 条 {self.plugin_name} 未识别的历史记录"
                )

        # 提取 history 中的 unique 值到一个集合中
        unique_flags = {h.get("unique") for h in history if h is not None}

        # 初始化豆瓣IP限制判断
        douban_last_ip_rate_limit_datetime = None
        douban_ip_rate_limit_times = 0

        for addr_index, _addr in enumerate(addr_list):
            if not _addr:
                continue
            try:
                logger.info(f"获取RSS：{_addr} ...")
                addr_result = DoubanRankPlus.__get_addr_save_paths(_addr)
                addr = addr_result.get("addr")
                customize_save_paths = addr_result.get("customize_save_paths")
                logger.debug(f"addr::: {addr}")
                logger.debug(f"customize_save_paths::: {customize_save_paths}")

                rss_infos = self.__get_rss_info(addr)
                if not rss_infos:
                    logger.error(f"RSS地址：{addr} ，未查询到数据")
                    continue
                else:
                    logger.info(f"RSS地址：{addr} ，共 {len(rss_infos)} 条数据")

                for rss_info_index, rss_info in enumerate(rss_infos):
                    if self._event.is_set():
                        logger.info("订阅服务停止")
                        return
                    mtype = None

                    logger.info(
                        f"第 {addr_index + 1}/{len(addr_list)} 条订阅数据处理进度: {rss_info_index + 1}/{len(rss_infos)}"
                    )

                    logger.debug(f"rss_info:::{rss_info}")
                    title = rss_info.get("title")
                    if not title:
                        logger.warn("标题为空，无法处理")
                        continue

                    douban_id = rss_info.get("doubanid")
                    year = rss_info.get("year")
                    type_str = rss_info.get("mtype")

                    if type_str == "movie":
                        mtype = MediaType.MOVIE
                    elif type_str:
                        mtype = MediaType.TV
                    unique_flag = (
                        f"{self.plugin_config_prefix}{title}_{year}_(DB:{douban_id})"
                    )
                    logger.debug(f"unique_flag:::{unique_flag}")

                    # 在集合中查找 unique_flag
                    if unique_flag in unique_flags:
                        logger.info(
                            f"已处理过: Title: {title}, Year:{year}, DBID:{douban_id}"
                        )
                        continue

                    logger.info(
                        f"开始处理: Title: {title}, Year:{year}, DBID:{douban_id}, Type:{mtype}"
                    )
                    # 元数据
                    meta = MetaInfo(title)
                    meta.year = year
                    if mtype:
                        meta.type = mtype
                    logger.debug(f"meta from MetaInfo:::{meta}")

                    # 豆瓣IP限制判断
                    if douban_last_ip_rate_limit_datetime:
                        if (
                            datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                            - douban_last_ip_rate_limit_datetime
                        ).seconds > 4200:
                            # 超过70分钟，重置
                            logger.info(
                                f"解除豆瓣IP限制, 上次触发时间为: {douban_last_ip_rate_limit_datetime}, 已触发次数: {douban_ip_rate_limit_times}"
                            )
                            douban_last_ip_rate_limit_datetime = None

                    # 识别媒体信息
                    if douban_id and not douban_last_ip_rate_limit_datetime:
                        # 随机休眠
                        random_sleep_time = round(
                            random.uniform(self._min_sleep_time, self._max_sleep_time),
                            1,
                        )

                        if random_sleep_time:
                            logger.info(
                                f"随机休眠范围: {self._min_sleep_time},{self._max_sleep_time}, 此次休眠时间: {random_sleep_time} 秒"
                            )
                            time.sleep(random_sleep_time)

                        # 识别豆瓣信息
                        if settings.RECOGNIZE_SOURCE == "themoviedb":
                            logger.info(
                                f"开始通过豆瓣ID {douban_id} 获取 {title} 的TMDB信息, 类型: {meta.type}"
                            )

                            tmdbinfo, is_ip_rate_limit = (
                                self.__get_tmdbinfo_by_doubanid(
                                    doubanid=douban_id, mtype=meta.type
                                )
                            )

                            if not tmdbinfo and not is_ip_rate_limit:
                                logger.warn(
                                    f"未识别到 {title} 的TMDB信息, 豆瓣ID: {douban_id} "
                                )
                                # 存储历史记录
                                history_payload = (
                                    DoubanRankPlus.__get_history_unrecognized_payload(
                                        title,
                                        unique_flag,
                                        year,
                                        douban_id,
                                    )
                                )
                                history.append(history_payload)
                                unique_flags.add(unique_flag)
                                logger.debug(f"已添加到历史：{history_payload}")
                                continue
                            elif is_ip_rate_limit:
                                logger.warn(
                                    f"未能从豆瓣获取数据, 触发豆瓣IP速率限制, 豆瓣ID: {douban_id}"
                                )
                                if self._is_exit_ip_rate_limit:
                                    logger.info("结束处理")
                                    return

                                douban_ip_rate_limit_times = (
                                    douban_ip_rate_limit_times + 1
                                )

                                logger.warn(
                                    f"70分钟时间内切换媒体识别。 上一次触发时间为: {douban_last_ip_rate_limit_datetime}, 已触发次数: {douban_ip_rate_limit_times}"
                                )

                                douban_last_ip_rate_limit_datetime = (
                                    datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                                )

                                logger.info(
                                    f"切换识别 {title} 的媒体信息, 类型: {meta.type}"
                                )
                                logger.debug(
                                    f"douban_last_ip_rate_limit_datetime:::{douban_last_ip_rate_limit_datetime}"
                                )

                                mediainfo = self.chain.recognize_media(
                                    meta=meta,
                                )
                                if not mediainfo:
                                    logger.warn(
                                        f"未识别到 {title} 的媒体信息, 豆瓣ID {douban_id}"
                                    )
                                    # 存储历史记录
                                    history_payload = DoubanRankPlus.__get_history_unrecognized_payload(
                                        title, unique_flag, year
                                    )
                                    history.append(history_payload)
                                    unique_flags.add(unique_flag)
                                    logger.debug(f"已添加到历史：{history_payload}")
                                    continue
                            else:
                                tmdbinfo_media_type = tmdbinfo.get("media_type", None)
                                tmdb_id = tmdbinfo.get("id", None)

                                logger.debug(
                                    f"从豆瓣ID {douban_id} 获得TMDB信息: TMDBID: {tmdb_id}, TMDBID Media Type: {tmdbinfo_media_type}"
                                )

                                if tmdbinfo_media_type:
                                    mtype = tmdbinfo_media_type
                                    meta.type = tmdbinfo_media_type

                                logger.info(
                                    f"继续通过TMDBID {tmdb_id} 识别 {title} 的媒体信息, 类型: {meta.type}"
                                )
                                mediainfo = self.chain.recognize_media(
                                    meta=meta,
                                    tmdbid=tmdb_id,
                                    mtype=meta.type,  # 直接使用类型查询tmdb详情
                                )

                                if not mediainfo:
                                    logger.warn(
                                        f"未识别到 {title} 的媒体信息, TMDBID: {tmdb_id} "
                                    )
                                    # 存储历史记录
                                    history_payload = DoubanRankPlus.__get_history_unrecognized_payload(
                                        title, unique_flag, year, douban_id
                                    )
                                    history.append(history_payload)
                                    unique_flags.add(unique_flag)
                                    logger.debug(f"已添加到历史：{history_payload}")
                                    continue

                        else:
                            logger.info(
                                f"开始通过豆瓣ID {douban_id} 识别 {title} 的媒体信息, 类型: {meta.type}"
                            )
                            mediainfo = self.chain.recognize_media(
                                meta=meta,
                                doubanid=douban_id,
                            )
                            if not mediainfo:
                                logger.warn(
                                    f"豆瓣ID {douban_id} 未识别到 {title} 的媒体信息"
                                )
                                # 存储历史记录
                                history_payload = (
                                    DoubanRankPlus.__get_history_unrecognized_payload(
                                        title, unique_flag, year, douban_id
                                    )
                                )
                                history.append(history_payload)
                                unique_flags.add(unique_flag)
                                logger.debug(f"已添加到历史：{history_payload}")
                                continue

                    else:
                        # 识别媒体信息
                        if douban_last_ip_rate_limit_datetime:
                            logger.info(
                                f"切换识别 {title} 的媒体信息, 类型: {meta.type}"
                            )
                        else:
                            logger.info(
                                f"开始识别 {title} 的媒体信息, 类型: {meta.type}"
                            )
                        mediainfo = self.chain.recognize_media(
                            meta=meta,
                        )
                        if not mediainfo:
                            logger.warn(
                                f"未识别到 {title} 的媒体信息, 豆瓣ID: {douban_id}"
                            )
                            # 存储历史记录
                            history_payload = (
                                DoubanRankPlus.__get_history_unrecognized_payload(
                                    title, unique_flag, year
                                )
                            )
                            history.append(history_payload)
                            unique_flags.add(unique_flag)
                            logger.debug(f"已添加到历史：{history_payload}")
                            continue

                    # logger.debug(f"{mediainfo}:::{mediainfo}")
                    logger.debug(f"{meta}:::{meta}")
                    logger.info(
                        f"已识别到 {title} ({year}) 的媒体信息: {mediainfo.title_year}, 类型: {mediainfo.type}"
                    )
                    # 保存路径
                    save_path = None
                    if customize_save_paths:
                        if mediainfo.type == MediaType.TV:
                            save_path = customize_save_paths["tv"]
                        elif mediainfo.type == MediaType.MOVIE:
                            save_path = customize_save_paths["movie"]

                    number_of_seasons = mediainfo.number_of_seasons
                    logger.debug(f"number_of_seasons:::{number_of_seasons}")

                    # 已识别状态默认值
                    status = Status.UNCATEGORIZED

                    # 如果是剧集且开启全季订阅，则轮流下载每一季
                    if (
                        self._is_seasons_all
                        and mediainfo.type == MediaType.TV
                        and number_of_seasons
                    ):
                        logger.debug(f"meta.begin_season:::{meta.begin_season}")
                        genre_ids = mediainfo.genre_ids
                        ANIME_GENRE_ID = 16
                        logger.debug(f"{mediainfo.title_year} genre_ids::: {genre_ids}")
                        if ANIME_GENRE_ID in genre_ids and customize_save_paths:
                            logger.info(
                                f"{mediainfo.title_year} 为动漫类别, 动漫自定义保存路径为: {customize_save_paths['anime']}"
                            )
                            save_path = customize_save_paths["anime"]

                        for i in range(1, number_of_seasons + 1):
                            logger.debug(
                                f"开始添加 {mediainfo.title_year} 第{i}/{number_of_seasons}季订阅"
                            )
                            __status = self.__checke_and_add_subscribe(
                                meta=meta,
                                mediainfo=mediainfo,
                                season=i,
                                save_path=save_path,
                            )
                            if not meta.begin_season or i == meta.begin_season:
                                status = __status
                    else:
                        status = self.__checke_and_add_subscribe(
                            meta=meta,
                            mediainfo=mediainfo,
                            season=meta.begin_season,
                            save_path=save_path,
                        )

                    # 存储历史记录
                    history_payload = {
                        "title": title,
                        "type": mediainfo.type.value,
                        "year": mediainfo.year,
                        "poster": mediainfo.get_poster_image(),
                        "overview": mediainfo.overview,
                        "tmdbid": str(mediainfo.tmdb_id) or "0",
                        "doubanid": douban_id or "0",
                        "unique": unique_flag,
                        "time": datetime.datetime.now(
                            tz=pytz.timezone(settings.TZ)
                        ).strftime("%m-%d %H:%M"),
                        "time_full": datetime.datetime.now(
                            tz=pytz.timezone(settings.TZ)
                        ).strftime("%Y-%m-%d %H:%M:%S"),
                        "vote": mediainfo.vote_average,
                        "status": status.value,
                    }
                    history.append(history_payload)
                    unique_flags.add(unique_flag)
                    logger.debug(f"已添加到历史：{history_payload}")

            except Exception as e:
                logger.error(f"处理RSS地址：{addr} 出错: {str(e)}")
            finally:
                # 保存历史记录
                logger.info(f"保存榜单 {addr} 处理后的历史记录")

                self.save_data("history", history)

        logger.info("所有榜单RSS刷新完成")

    def __checke_and_add_subscribe(
        self,
        meta,
        mediainfo,
        season,
        save_path,
    ) -> Status:
        if save_path:
            logger.info(f"{mediainfo.title_year} 的自定义保存路径为: {save_path}")

        # 判断上映年份是否符合要求
        if self._release_year and int(mediainfo.year) < self._release_year:
            logger.info(
                f"{mediainfo.title_year} 上映年份: {mediainfo.year}, 不符合要求"
            )
            return Status.YEAR_NOT_MATCH
        # 判断评分是否符合要求
        if self._vote and mediainfo.vote_average < self._vote:
            logger.info(
                f"{mediainfo.title_year} 评分: {mediainfo.vote_average}, 不符合要求"
            )
            return Status.RATING_NOT_MATCH

        # 查询缺失的媒体信息
        exist_flag, _ = self.downloadchain.get_no_exists_info(
            meta=meta, mediainfo=mediainfo
        )
        if exist_flag:
            logger.info(f"{mediainfo.title_year} 媒体库中已存在")
            return Status.MEDIA_EXISTS

        # 判断用户是否已经添加订阅
        if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
            logger.info(f"{mediainfo.title_year} 订阅已存在")
            return Status.SUBSCRIPTION_EXISTS

        # 添加订阅
        self.subscribechain.add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=season,
            exist_ok=True,
            username=self.plugin_name,
            save_path=save_path,
        )
        logger.info(f"已添加订阅: {mediainfo.title_year}")
        return Status.SUBSCRIPTION_ADDED

    def __get_rss_info(self, addr) -> List[RssInfo]:
        """
        获取RSS
        """
        try:
            if self._proxy:
                ret = RequestUtils(timeout=240, proxies=settings.PROXY).get_res(addr)
            else:
                ret = RequestUtils(timeout=240).get_res(addr)
            if not ret:
                return []
            ret_xml = ret.text
            ret_array: List[RssInfo] = []

            # 解析XML
            dom_tree = xml.dom.minidom.parseString(ret_xml)
            rootNode = dom_tree.documentElement
            items = rootNode.getElementsByTagName("item")
            for item in items:
                try:
                    # 标题
                    title = DomUtils.tag_value(item, "title", default="")
                    # 链接
                    link = DomUtils.tag_value(item, "link", default="")
                    if not title and not link:
                        logger.warn("条目标题和链接均为空，无法处理")
                        continue

                    # 豆瓣ID
                    found_doubanid = re.findall(r"/(\d+)/", link)
                    if found_doubanid:
                        doubanid = found_doubanid[0]
                        if not str(doubanid).isdigit():
                            logger.warn(f"解析的豆瓣ID格式不正确：{doubanid}")
                            continue
                    else:
                        doubanid = None

                    # 年份
                    year = DomUtils.tag_value(item, "year", default="")
                    if not year:
                        # 年份
                        description = DomUtils.tag_value(
                            item, "description", default=""
                        )
                        # 删除 '评价数' 到第一个 '<br>' 之间的字符串
                        description = re.sub(r"评价数.*?<br>", "", description)
                        # 删除所有 <img> 标签及其内容
                        description = re.sub(r"<img.*?>", "", description)
                        # 匹配4位独立数字1900-2099年
                        found_year = re.findall(r"\b(19\d{2}|20\d{2})\b", description)
                        year = found_year[0] if found_year else None

                    # 类型
                    mtype = DomUtils.tag_value(item, "type", default="")

                    rss_info: RssInfo = {
                        "title": title,
                        "link": link,
                        "mtype": mtype,
                        "year": year,
                        "doubanid": doubanid,
                    }
                    # 返回对象
                    ret_array.append(rss_info)

                except Exception as e1:
                    logger.error("解析RSS条目失败：" + str(e1))
                    continue
            return ret_array
        except Exception as e:
            logger.error("获取RSS失败：" + str(e))
            return []

    @staticmethod
    def __get_addr_save_paths(addr: str) -> Dict[str, Dict[str, str] | str | None]:
        # 提取分号分割的链接和保存地址
        if ";" not in addr:
            return {"addr": addr, "customize_save_path": None}
        else:
            logger.debug("分割订阅地址")
            split_str = addr.split(";")
            str_list: List[str] = []
            for item in split_str:
                if item.strip():
                    str_list.append(item.strip())
            addr = str_list[0]
            customize_save_path = str_list[1]

            logger.debug(f"addr: {addr}")
            logger.debug("customize_save_path:" f" {customize_save_path}")

            if "#" in customize_save_path:
                customize_save_path_list = customize_save_path.split("#")

                logger.debug("customize_save_path_list:" f" {customize_save_path_list}")

                customize_save_path_movie = customize_save_path_list[0]
                customize_save_path_tv = customize_save_path_list[1]
                customize_save_path_anime = (
                    customize_save_path_list[2] or customize_save_path_tv
                )

                logger.debug(
                    f"订阅链接 {addr} 的自定义保存路径为:"
                    f" 电影:{customize_save_path_movie},"
                    f" 电视剧: {customize_save_path_tv},"
                    f" 动漫: {customize_save_path_anime}"
                )

            else:
                customize_save_path_movie = customize_save_path
                customize_save_path_tv = customize_save_path
                customize_save_path_anime = customize_save_path

                logger.debug(
                    f"订阅链接 {addr} 的自定义保存路径为:" f" {customize_save_path}"
                )
            customize_save_paths = {
                "movie": customize_save_path_movie,
                "tv": customize_save_path_tv,
                "anime": customize_save_path_anime,
            }
            return {"addr": addr, "customize_save_paths": customize_save_paths}

    @staticmethod
    def __get_history_unrecognized_payload(
        title: str,
        unique: str,
        year: str | None = None,
        doubanid: str | None = None,
    ) -> HistoryPayload:
        """
        获取历史记录
        """
        history_payload: HistoryPayload = {
            "title": title,
            "unique": unique,
            "status": Status.UNRECOGNIZED.value,
            "type": MediaType.UNKNOWN.value,
            "year": year or "0",
            "poster": "/assets/no-image-CweBJ8Ee.jpeg",
            "overview": "",
            "tmdbid": "0",
            "doubanid": doubanid or "0",
            "time": datetime.datetime.now(tz=pytz.timezone(settings.TZ)).strftime(
                "%m-%d %H:%M"
            ),
            "time_full": datetime.datetime.now(tz=pytz.timezone(settings.TZ)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "vote": 0.0,
        }
        return history_payload

    def __get_tmdbinfo_by_doubanid(
        self, doubanid: str, mtype: MediaType | None = None
    ) -> Tuple[dict[str, Any] | None, bool]:
        """
        根据豆瓣ID获取TMDB信息
        """
        doubaninfo, is_ip_rate_limit = self.__douban_info(
            doubanid=doubanid, mtype=mtype
        )
        if is_ip_rate_limit or not doubaninfo:
            return None, is_ip_rate_limit

        # 优先使用原标题匹配
        title = doubaninfo.get("title", "")
        original_title = doubaninfo.get("original_title", "")
        meta = MetaInfo(title=original_title if original_title else title)

        # 年份
        meta.year = doubaninfo.get("year")

        # 处理类型
        media_type = doubaninfo.get("media_type")
        media_type = (
            media_type
            if isinstance(media_type, MediaType)
            else MediaType.MOVIE if doubaninfo.get("type") == "movie" else MediaType.TV
        )
        meta.type = media_type

        # 匹配TMDB信息
        if original_title:
            meta_names = list(
                dict.fromkeys([original_title, title, meta.cn_name, meta.en_name])
            )
        else:
            meta_names = list(dict.fromkeys([title, meta.cn_name, meta.en_name]))

        # 移除空值
        meta_names = [name for name in meta_names if name]

        __mtype = mtype if mtype and mtype != MediaType.UNKNOWN else meta.type

        for name in meta_names:
            tmdbinfo = self.mediachain.match_tmdbinfo(
                name=name,
                year=meta.year,
                mtype=__mtype,
                season=meta.begin_season,
            )
            if tmdbinfo:
                # 合季季后返回
                tmdbinfo["season"] = meta.begin_season
                return tmdbinfo, is_ip_rate_limit

        return None, is_ip_rate_limit

    def __douban_info(
        self, doubanid: str, mtype: MediaType | None = None
    ) -> Tuple[dict[str, Any] | None, bool]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :param mtype:    媒体类型
        :return: 豆瓣信息
        """
        """
        豆瓣IP速率限制错误信息
        {'msg': 'subject_ip_rate_limit', 'code': 1309, 'request': 'GET /v2/movie/30483637', 'localized_message': '您所在的网络存在异常，请登录后重试。'}
        """

        def __douban_tv() -> Tuple[dict[str, Any] | None, bool]:
            """
            获取豆瓣剧集信息
            """
            info = self.doubanapi.tv_detail(doubanid)
            if info:
                if "subject_ip_rate_limit" in info.get("msg", ""):
                    logger.warn(f"触发豆瓣IP速率限制，错误信息：{info} ...")
                    return None, True
            return info, False

        def __douban_movie() -> Tuple[dict[str, Any] | None, bool]:
            """
            获取豆瓣电影信息
            """
            info = self.doubanapi.movie_detail(doubanid)
            if info:
                if "subject_ip_rate_limit" in info.get("msg", ""):
                    logger.warn(f"触发豆瓣IP速率限制，错误信息：{info} ...")
                    return None, True
            return info, False

        if not doubanid:
            return None, False
        logger.info(f"开始获取豆瓣信息：{doubanid} ...")
        if mtype == MediaType.TV:
            return __douban_tv()
        else:
            movie_info, is_ip_rate_limit = __douban_movie()
            if not movie_info and not is_ip_rate_limit:
                logger.debug("未从电影类型获取到信息，返回从剧集获取信息")
                return __douban_tv()
            else:
                return movie_info, is_ip_rate_limit
