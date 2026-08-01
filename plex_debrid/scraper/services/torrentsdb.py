# import modules
from base import *
from ui.ui_print import *
from scraper.services import torrentio

name = "torrentsdb"

# TorrentsDB 的响应与 torrentio 逐字段同构(title 里 💾 体积 / 👤 做种 / ⚙️ 索引器,
# 外加 infoHash),所以整套解析直接复用 torrentio.scrape_with,不另写一份。
# 它多出 knaben / rargb / 1lou 等 torrentio 没有的索引器,冷门片和非英语片的候选池明显更宽;
# 串台结果由 content/classes.py 的 deviation 片名闸拦下(已实测)。
default_opts = "https://torrentsdb.com/manifest.json"


def setup(cls, new=False):
    return torrentio.setup(cls, new)


def scrape(query, altquery):
    return torrentio.scrape_with(query, altquery, name, default_opts)
