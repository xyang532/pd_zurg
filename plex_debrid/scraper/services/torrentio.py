# import modules
from base import *
from ui.ui_print import *
import releases

name = "torrentio"

default_opts = "https://torrentio.strem.fun/sort=qualitysize|qualityfilter=480p,scr,cam/manifest.json"

session = custom_session()


def base_and_opts(manifest_url, fallback="https://torrentio.strem.fun"):
    """把 manifest URL 拆成 (基址, 参数段)。

    上游原来写死了 torrentio.strem.fun,只从 URL 里取参数段 —— 于是设置项虽然说
    "填一个 manifest url",填别的主机名也没用。拆开之后,任何 torrentio 同构的源站
    (TorrentsDB 等)都能靠一份代码接进来,不用再复制一遍这个文件。
    """
    url = manifest_url.strip()
    if url.endswith("manifest.json"):
        url = url[: -len("manifest.json")]
    url = url.rstrip("/")
    m = regex.match(r'(https?://[^/]+)(?:/(.*))?$', url)
    if not m:
        return fallback, ""
    return m.group(1), (m.group(2) or "")


def get(url, tag=name):
    headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'}
    try:
        ui_print(f"[{tag}] GET url: {url} ...", ui_settings.debug)
        response = session.get(url, headers=headers, timeout=60)
        ui_print("done", ui_settings.debug)
        if hasattr(response, "status_code") and response.status_code != 200:
            ui_print(f'[{tag}] error {str(response.status_code)}: failed response from {tag}. {response.content.decode("utf-8")}')
        response = json.loads(
            response.content, object_hook=lambda d: SimpleNamespace(**d))
        return response
    except Exception as e:
        ui_print(f'[{tag}] error: ' + str(e))
        return None


def setup(cls, new=False):
    from settings import settings_list
    from scraper.services import active
    settings = []
    for category, allsettings in settings_list:
        for setting in allsettings:
            if setting.cls == cls:
                settings += [setting]
    if settings == []:
        if not cls.name in active:
            active += [cls.name]
    back = False
    if not new:
        while not back:
            print("0) Back")
            indices = []
            for index, setting in enumerate(settings):
                print(str(index + 1) + ') ' + setting.name)
                indices += [str(index + 1)]
            print()
            if settings == []:
                print("Nothing to edit!")
                print()
                time.sleep(3)
                return
            choice = input("Choose an action: ")
            if choice in indices:
                settings[int(choice) - 1].input()
                if not cls.name in active:
                    active += [cls.name]
                back = True
            elif choice == '0':
                back = True
    else:
        if not cls.name in active:
            active += [cls.name]


def scrape(query, altquery):
    return scrape_with(query, altquery, name, default_opts)


def scrape_with(query, altquery, svc, manifest_url):
    """torrentio 协议的通用抓取。svc = 源站名(要和 services.active 里的名字一致)。"""
    from scraper.services import active
    scraped_releases = []
    if not svc in active:
        return scraped_releases
    if altquery == "(.*)":
        altquery = query
    type = ("show" if regex.search(
        r'(S[0-9]|complete|S\?[0-9])', altquery, regex.I) else "movie")
    base, opts = base_and_opts(manifest_url)
    prefix = base + '/' + opts + ("/" if len(opts) > 0 else "")
    if type == "show":
        s = (regex.search(r'(?<=S)([0-9]+)', altquery, regex.I).group()
             if regex.search(r'(?<=S)([0-9]+)', altquery, regex.I) else None)
        e = (regex.search(r'(?<=E)([0-9]+)', altquery, regex.I).group()
             if regex.search(r'(?<=E)([0-9]+)', altquery, regex.I) else None)
        if s == None or int(s) == 0:
            s = 1
        if e == None or int(e) == 0:
            e = 1
    plain_text = ""
    if regex.search(r'(tt[0-9]+)', altquery, regex.I):
        query = regex.search(r'(tt[0-9]+)', altquery, regex.I).group()
    else:
        plain_text = copy.deepcopy(query)
        try:
            if type == "show":
                url = "https://v3-cinemeta.strem.io/catalog/series/top/search=" + query + ".json"
                meta = get(url, svc)
            else:
                url = "https://v3-cinemeta.strem.io/catalog/movie/top/search=" + query + ".json"
                meta = get(url, svc)
            query = meta.metas[0].imdb_id
        except:
            try:
                if type == "movie":
                    type = "show"
                    s = 1
                    e = 1
                    url = "https://v3-cinemeta.strem.io/catalog/series/top/search=" + query + ".json"
                    meta = get(url, svc)
                else:
                    type = "movie"
                    url = "https://v3-cinemeta.strem.io/catalog/movie/top/search=" + query + ".json"
                    meta = get(url, svc)
                query = meta.metas[0].imdb_id
            except Exception as e:
                ui_print(f'[{svc}] error: could not find IMDB ID. ' + str(e))
                return scraped_releases
    if type == "movie":
        url = prefix + 'stream/movie/' + query + '.json'
        response = get(url, svc)
        if not hasattr(response, "streams") or len(response.streams) == 0:
            type = "show"
            s = 1
            e = 1
            if plain_text != "":
                try:
                    url = "https://v3-cinemeta.strem.io/catalog/series/top/search=" + plain_text + ".json"
                    meta = get(url, svc)
                    query = meta.metas[0].imdb_id
                except Exception as e:
                    ui_print(f'[{svc}] error: could not find IMDB ID. ' + str(e))
                    return scraped_releases
    if type == "show":
        url = prefix + 'stream/series/' + \
            query + ':' + str(int(s)) + ':' + str(int(e)) + '.json'
        response = get(url, svc)
    if not hasattr(response, "streams"):
        try:
            if not response == None:
                ui_print(f'[{svc}] error: ' + str(response))
        except Exception as e:
            ui_print(f'[{svc}] error: unknown error. ' + str(e))
        return scraped_releases
    elif len(response.streams) == 1 and not hasattr(response.streams[0], "infoHash"):
        ui_print(f'[{svc}] error: "' + response.streams[0].name.replace('\n',
                 ' ') + '" - ' + response.streams[0].title.replace('\n', ' '))
        return scraped_releases
    ui_print(f"[{svc}] found {str(len(response.streams))} streams", ui_settings.debug)
    for result in response.streams:
        try:
            title = result.title.split('\n')[0].replace(' ', '.')
            size = (float(regex.search(r'(?<=💾 )([0-9]+.?[0-9]+)(?= GB)', result.title).group()) if regex.search(r'(?<=💾 )([0-9]+.?[0-9]+)(?= GB)', result.title) else float(
                regex.search(r'(?<=💾 )([0-9]+.?[0-9]+)(?= MB)', result.title).group())/1000 if regex.search(r'(?<=💾 )([0-9]+.?[0-9]+)(?= MB)', result.title) else 0)
            links = ['magnet:?xt=urn:btih:' + result.infoHash + '&dn=&tr=']
            seeds = (int(regex.search(r'(?<=👤 )([0-9]+)', result.title).group(
            )) if regex.search(r'(?<=👤 )([1-9]+)', result.title) else 0)
            source = ((regex.search(r'(?<=⚙️ )(.*)(?=\n|$)', result.title).group())
                      if regex.search(r'(?<=⚙️ )(.*)(?=\n|$)', result.title) else "unknown")
            scraped_releases += [releases.release(
                '['+svc+': '+source+']', 'torrent', title, [], size, links, seeds)]
        except:
            continue
    return scraped_releases
