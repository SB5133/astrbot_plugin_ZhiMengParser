from random import choice

from msgspec import Struct, field


class PlayAddr(Struct):
    url_list: list[str]


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    # avatar_larger: Avatar
    avatar_thumb: Avatar


class Statistics(Struct):
    """统计数据（字段在部分页面可能缺失或为字符串，宽松处理）"""

    digg_count: int | str | None = None
    """点赞数"""
    comment_count: int | str | None = None
    """评论数"""
    collect_count: int | str | None = None
    """收藏数"""
    share_count: int | str | None = None
    """分享数"""
    play_count: int | str | None = None
    """播放数"""


class SlidesData(Struct):
    author: Author
    desc: str
    create_time: int
    images: list[Image]
    statistics: Statistics | None = None

    @property
    def name(self) -> str:
        return self.author.nickname

    @property
    def avatar_url(self) -> str:
        return choice(self.author.avatar_thumb.url_list)

    @property
    def image_urls(self) -> list[str]:
        return [choice(image.url_list) for image in self.images]

    @property
    def dynamic_urls(self) -> list[str]:
        return [
            choice(image.video.play_addr.url_list)
            for image in self.images
            if image.video
        ]


class SlidesInfo(Struct):
    aweme_details: list[SlidesData] = field(default_factory=list)
