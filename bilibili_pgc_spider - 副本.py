# -*- coding: utf-8 -*-
"""
Bilibili 番剧视频爬虫脚本
==========================

功能概述：
    本脚本用于从 Bilibili（哔哩哔哩）番剧页面下载视频资源。
    支持从 HTML 页面中提取播放地址（playinfo），自动选择最高画质，
    并通过 FFmpeg 合并分离的音视频流为完整的 MP4 文件。

主要特性：
    - 支持 PGC 番剧页面（bangumi/play/epXXX）和普通视频页面
    - 自动从页面 HTML 中提取多种格式的 playinfo JSON 数据
    - 列出所有可选画质，用户可自由选择，默认最高画质
    - 支持 Cookie 登录以获取更高画质资源
    - 实时显示下载进度
    - 合并前询问用户是否使用 FFmpeg 合并音视频

使用方式：
    直接运行脚本，按提示输入视频 URL 和 Cookie 即可。
    也可将 Cookie 保存到同目录下的 cookie.txt 文件中自动读取。

输出目录：
    脚本所在目录下的 downloads/ 文件夹
"""

import json                     # JSON 数据解析与序列化
import os                       # 操作系统接口（文件路径、目录操作等）
import re                       # 正则表达式，用于字符串匹配与提取
import sys                      # 系统相关参数与函数（用于刷新输出等）
import shutil                   # 高阶文件操作（用于查找 ffmpeg 可执行文件路径）
import requests                 # HTTP 请求库，用于发送网络请求
import logging                  # 日志记录模块
import subprocess               # 子进程管理（备用命令行调用 FFmpeg）
from urllib.parse import urlparse  # URL 解析工具，用于提取路径、扩展名等

# 尝试导入 ffmpeg-python 库，如果未安装则 fallback 到命令行方式
try:
    import ffmpeg               # FFmpeg Python 绑定库，用于音视频处理
    _HAS_FFMPEG_PY = True       # 标记 ffmpeg-python 库是否可用
except ImportError:
    ffmpeg = None               # 未安装时设为 None
    _HAS_FFMPEG_PY = False      # 标记为不可用


# 脚本所在目录的绝对路径，作为所有相对路径的基准
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==================== FFmpeg 路径配置 ====================
# FFmpeg 可执行文件路径，优先级：
# 1. 环境变量 FFMPEG_PATH（如果设置了的话）
# 2. shutil.which('ffmpeg') 在系统 PATH 中查找
# 3. 空字符串（表示未找到，后续合并功能将被跳过）
# 如需手动指定，直接修改下一行：例如 FFMPEG_PATH = r'C:\ffmpeg\bin\ffmpeg.exe'
FFMPEG_PATH = os.environ.get('FFMPEG_PATH', '') or shutil.which('ffmpeg') or ''

# 日志文件路径：脚本所在目录下的 bilibili_spider.txt
LOG_PATH = os.path.join(BASE_DIR, "bilibili_spider.txt")

# ==================== 日志配置 ====================
# 创建日志记录器，名称为 "bili_spider"
logger = logging.getLogger("bili_spider")
# 设置日志级别为 INFO（输出 INFO 及以上级别的日志）
logger.setLevel(logging.INFO)
# 定义日志输出格式：时间 - 级别 - 消息内容
fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

# ===== 控制台输出处理器 =====
# 创建控制台输出处理器（将日志输出到终端）
ch = logging.StreamHandler()
# 为处理器设置输出格式
ch.setFormatter(fmt)
# 将处理器添加到日志记录器中
logger.addHandler(ch)

# ===== 文件输出处理器 =====
# 创建文件输出处理器（将日志追加写入到文件中，使用 UTF-8 编码）
fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
# 为文件处理器设置同样的输出格式
fh.setFormatter(fmt)
# 将文件处理器添加到日志记录器中
logger.addHandler(fh)


def sanitize_filename(name, fallback="file"):
    """
    清理文件名，移除操作系统不允许的字符，使其可安全用于文件保存。

    不同操作系统对文件名有不同的限制（如 Windows 不允许 <>:"/\\|?* 等字符），
    本函数将这些非法字符全部移除，同时清理末尾的点号和空白字符。

    参数:
        name (str):
            原始文件名，可以是任意字符串。函数会先取其 basename（去掉目录路径）。
        fallback (str, 可选):
            当清理后的文件名为空时使用的默认名称。
            默认值为 "file"。

    返回:
        str:
            清理后的安全文件名。如果结果为空字符串，则返回 fallback 参数的值。

    示例:
        >>> sanitize_filename('我的视频/第一集: 开篇?')
        '第一集 开篇'
        >>> sanitize_filename('   ...   ')
        'file'
    """
    # 取路径的 basename（去除目录部分），转为字符串并去除首尾空白
    safe = os.path.basename(str(name)).strip()
    # 移除 Windows / 多数文件系统不允许的字符：< > : " / \ | ? * 以及控制字符 \x00-\x1f
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '', safe)
    # 移除文件名末尾的点号和空白字符（Windows 不允许文件名以点结尾）
    safe = re.sub(r'[.\s]+$', '', safe)
    # 如果结果非空则返回，否则返回 fallback 默认值
    return safe or fallback


def cookie_str_to_dict(cookie_string):
    """
    将浏览器格式的 Cookie 字符串转换为 Python 字典，方便 requests 库使用。

    浏览器中的 Cookie 通常以 "key1=value1; key2=value2" 的字符串形式存在，
    而 requests 库的 cookies 参数接受字典格式，因此需要进行转换。

    参数:
        cookie_string (str):
            Cookie 字符串，格式为 "key1=value1; key2=value2; ..."。

    返回:
        dict:
            转换后的 Cookie 字典，键为 cookie 名，值为 cookie 值。

    示例:
        >>> cookie_str_to_dict('SESSDATA=xxx; bili_jct=yyy')
        {'SESSDATA': 'xxx', 'bili_jct': 'yyy'}
    """
    # 使用字典推导式：
    # 1. 按 "; " 分割 cookie 字符串为多个键值对
    # 2. 对每个键值对按 "=" 分割为 key 和 value（仅分割第一个 =，因为 value 中可能包含 =）
    # 3. 只处理包含 "=" 的有效键值对
    return {pair.split('=', 1)[0]: pair.split('=', 1)[1] for pair in cookie_string.split('; ') if '=' in pair}


def validate_cookie(cookie_string, headers):
    """
    验证 Cookie 是否有效（即是否处于登录状态）。

    通过调用 Bilibili 的用户导航接口 /x/web-interface/nav 来判断 Cookie 是否有效。
    该接口返回用户信息，如果 isLogin 为 True 则表示 Cookie 有效。

    参数:
        cookie_string (str):
            待验证的 Cookie 字符串，格式为 "key1=value1; key2=value2"。
            如果为空字符串，则直接返回 False。
        headers (dict):
            请求头字典，用于设置 User-Agent 等基础请求信息。
            函数内部会复制一份并添加 Cookie 字段。

    返回:
        bool:
            Cookie 有效且已登录返回 True，否则返回 False。

    说明:
        - Cookie 无效或网络异常时，会输出警告日志但不抛出异常
        - 主要用于告知用户当前 Cookie 状态，不影响后续下载流程
    """
    # 如果未提供 cookie，直接返回 False
    if not cookie_string:
        logger.warning("没有提供 cookie，使用匿名请求。")
        return False

    # 复制一份 headers，避免修改原始字典
    check_headers = dict(headers)
    # 在请求头中添加 Cookie 字段
    check_headers['Cookie'] = cookie_string

    try:
        # 调用 Bilibili 用户导航接口，获取当前登录状态
        r = requests.get('https://api.bilibili.com/x/web-interface/nav', headers=check_headers, timeout=10)
        # 如果 HTTP 状态码不是 2xx，抛出异常
        r.raise_for_status()
        # 解析返回的 JSON 数据
        data = r.json()
        # 检查 isLogin 字段是否为 True（使用 .get 链式调用避免键不存在时报错）
        if data.get('data', {}).get('isLogin'):
            logger.info('Cookie 有效，已登录')
            return True
        # isLogin 为 False 或不存在，表示未登录
        logger.warning('Cookie 未登录或无效')
        return False
    except Exception as e:
        # 网络请求失败或解析失败时，输出警告并返回 False
        logger.warning(f'验证 cookie 时异常: {e}')
        return False


def extract_json_after_prefix(html, prefix):
    """
    从 HTML 文本中提取指定前缀后面的 JSON 对象。

    Bilibili 页面会在 JavaScript 中嵌入播放地址等 JSON 数据，
    但正则表达式难以正确处理嵌套的 JSON（尤其是多层大括号嵌套的情况）。
    本函数通过逐字符扫描、维护大括号深度计数器的方式，
    准确提取完整的 JSON 对象，即使 JSON 内部包含字符串中的大括号也能正确识别。

    处理逻辑：
        1. 在 HTML 中查找指定前缀字符串的位置
        2. 跳过后缀后的空白字符，定位到 JSON 的起始 "{"
        3. 逐字符遍历，维护大括号深度计数器
        4. 正确处理字符串中的大括号和转义字符（避免误判）
        5. 当深度计数器回到 0 时，表示找到了完整的 JSON 对象
        6. 尝试解析并返回结果

    参数:
        html (str):
            页面 HTML 源码文本。
        prefix (str):
            要查找的 JSON 前缀字符串，例如 "window.__playinfo__ ="。
            函数会提取该前缀后面紧跟着的 JSON 对象。

    返回:
        dict or None:
            成功提取并解析返回 JSON 字典；
            如果找不到前缀、前缀后不是合法 JSON、或 JSON 解析失败，则返回 None。
    """
    # 在 HTML 中查找前缀字符串的起始位置
    idx = html.find(prefix)
    if idx == -1:
        # 未找到前缀，返回 None
        return None

    # 计算前缀结束后的起始位置（跳过前缀本身）
    start = idx + len(prefix)
    # 跳过前缀和 JSON 之间的空白字符（空格、制表符、换行符、回车符）
    while start < len(html) and html[start] in ' \t\n\r':
        start += 1

    # 检查起始字符是否为 "{"，即是否为 JSON 对象的开始
    if start >= len(html) or html[start] != '{':
        return None

    # 大括号深度计数器，初始为 0
    depth = 0
    # 当前扫描位置，从 JSON 起始位置开始
    i = start
    # 是否处于字符串内部（字符串内的大括号不计入深度）
    in_string = False
    # 前一个字符是否为转义符 "\"（转义后的引号不结束字符串）
    escape = False

    # 逐字符扫描 HTML
    while i < len(html):
        ch = html[i]

        if escape:
            # 前一个字符是转义符，当前字符被转义，跳过
            escape = False
        elif ch == '\\':
            # 遇到转义符，标记下一个字符为转义状态
            escape = True
        elif ch == '"':
            # 遇到双引号，切换字符串内部/外部状态
            in_string = not in_string
        elif not in_string:
            # 只有在字符串外部的大括号才影响深度计数
            if ch == '{':
                # 左大括号，深度 +1
                depth += 1
            elif ch == '}':
                # 右大括号，深度 -1
                depth -= 1
                # 深度回到 0，表示找到完整的 JSON 对象
                if depth == 0:
                    try:
                        # 提取从起始位置到当前右大括号（包含）的子串并解析为 JSON
                        return json.loads(html[start:i+1])
                    except json.JSONDecodeError:
                        # JSON 解析失败，返回 None
                        return None
        # 继续扫描下一个字符
        i += 1

    # 扫描到 HTML 末尾仍未找到完整的 JSON，返回 None
    return None


def extract_playinfo_from_html(html):
    """
    从 Bilibili 页面 HTML 中提取播放信息（playinfo）JSON 数据。

    Bilibili 不同类型的页面（番剧、普通视频、新版/旧版页面）会将播放地址数据
    嵌入在不同的 JavaScript 变量中。本函数尝试多种可能的前缀，
    只要找到任意一种就返回对应的 JSON 数据。

    目前支持的前缀格式（按优先级排序）：
        1. const playurlSSRData =          —— 番剧页面 SSR 数据（服务端渲染）
        2. playurlSSRData =                 —— 同上的简写形式
        3. window.__PLAYURL_HYDRATE_DATA__ = —— 新版页面水合数据
        4. window.__playinfo__ =            —— 普通视频页面
        5. __playinfo__ =                   —— 同上的简写形式

    参数:
        html (str):
            Bilibili 视频/番剧页面的 HTML 源码。

    返回:
        dict or None:
            成功提取返回 playinfo 字典；
            如果所有前缀都没找到，返回 None。
    """
    # 可能的 JSON 前缀列表，按优先级从高到低排列
    prefixes = [
        'const playurlSSRData =',
        'playurlSSRData =',
        'window.__PLAYURL_HYDRATE_DATA__ =',
        'window.__playinfo__ =',
        '__playinfo__ =',
    ]
    # 逐个尝试每个前缀
    for p in prefixes:
        data = extract_json_after_prefix(html, p)
        if data:
            # 找到第一个有效的就立即返回
            return data
    # 所有前缀都没找到，返回 None
    return None


def get_playinfo_normalized(parsed):
    """
    归一化（标准化）不同格式的 playinfo 数据，统一访问接口。

    Bilibili 不同页面类型返回的 playinfo JSON 结构各不相同，
    数据可能嵌套在不同层级中，例如：
        - 普通视频：直接包含 dash / durl
        - 番剧页面：data -> result -> dash
        - 某些番剧：data -> result -> video_info -> dash
        - 某些接口：result -> dash

    本函数将这些不同结构的数据统一整理成一个标准字典，
    保证返回的字典中可以直接通过 ['dash'] 或 ['durl'] 访问播放地址信息，
    而无需关心原始数据的嵌套层级。

    参数:
        parsed (dict):
            从 HTML 中提取的原始 playinfo 字典（可能是任意结构）。
            如果为 None 或空，直接返回 None。

    返回:
        dict or None:
            归一化后的播放信息字典，结构形如：
            {
                'dash': {
                    'video': [...],  # 视频流列表
                    'audio': [...],  # 音频流列表
                },
                'durl': [...],        # 直链列表（旧格式，音视频不分离）
                ...  # 其他原始字段保留
            }
            如果无法从输入中找到 dash 或 durl，则返回 None。
    """
    # 输入为空直接返回 None
    if not parsed:
        return None

    # 情况1：数据外层有 'data' 字段
    if isinstance(parsed.get('data'), dict):
        data = parsed['data']

        # 情况1a：data 内部还有 'result' 字段（番剧页面常见结构）
        if isinstance(data.get('result'), dict):
            res = data['result']
            # 番剧页面的 dash/durl 可能在 'video_info' 子字段内
            vi = res.get('video_info', {})
            # 判断 result 或 video_info 中是否存在 dash
            has_dash = bool(res.get('dash')) or bool(vi.get('dash'))
            # 判断 result 或 video_info 中是否存在 durl
            has_durl = bool(res.get('durl')) or bool(vi.get('durl'))

            # 如果有 dash 或 durl 任一种格式
            if has_dash or has_durl:
                # 如果 dash 在 video_info 里而不在 result 里，提升到 result 层
                if not res.get('dash') and vi.get('dash'):
                    res['dash'] = vi['dash']
                # 如果 durl 在 video_info 里而不在 result 里，提升到 result 层
                if not res.get('durl') and vi.get('durl'):
                    res['durl'] = vi['durl']
                # 返回归一化后的 result（顶层可直接访问 dash/durl）
                return res

        # 情况1b：data 层直接包含 dash 或 durl（普通视频页面常见）
        if 'dash' in data or 'durl' in data:
            return data

    # 情况2：最外层直接包含 dash 或 durl
    if 'dash' in parsed or 'durl' in parsed:
        return parsed

    # 情况3：最外层有 result 字段（某些接口格式）
    if isinstance(parsed.get('result'), dict):
        return parsed['result']

    # 以上情况都不匹配，返回 None
    return None


def list_qualities(playinfo):
    """
    列出所有可用的画质选项，按清晰度从高到低排序。

    遍历 playinfo 中的 DASH 视频流和 durl 直链，
    将所有可用的画质选项整理成一个列表，供用户选择。
    DASH 格式按清晰度 ID 分组去重（同一清晰度可能有 H.264/H.265/AV1 等多种编码），
    每组中选择带宽最高的视频流（通常是质量最好的编码），
    所有视频流都搭配最高质量的音频流。

    参数:
        playinfo (dict):
            归一化后的播放信息字典（由 get_playinfo_normalized 返回）。
            应包含 'dash' 或 'durl' 字段。

    返回:
        list:
            画质选项列表，每个元素是一个字典，包含：
            - type (str): 类型，'dash' 或 'durl'
            - video (dict): DASH 视频流信息（仅 dash 类型）
            - audio (dict): DASH 音频流信息（仅 dash 类型，可能为 None）
            - durl (dict): durl 直链信息（仅 durl 类型）
            - desc (str): 画质描述字符串，用于显示给用户
            列表按清晰度从高到低排序。
    """
    # 获取 DASH 数据
    dash = playinfo.get('dash', {})
    # DASH 视频流列表
    vlist = dash.get('video', [])
    # DASH 音频流列表
    alist = dash.get('audio', [])
    # 画质选项列表
    qualities = []

    # 处理 DASH 格式的视频流
    if vlist:
        # 预先选出最高质量的音频流（所有画质共用最高质量音频）
        best_a = max(alist, key=lambda x: x.get('bandwidth', 0)) if alist else None
        # 按清晰度 ID 分组，同一清晰度可能有多种编码（H.264/H.265/AV1）
        quality_groups = {}
        for v in vlist:
            # 清晰度 ID：id 字段优先，没有的话用 quality 字段
            qid = v.get('id', v.get('quality', 0))
            # 同一清晰度组中保留带宽最高的那个视频流
            if qid not in quality_groups or v.get('bandwidth', 0) > quality_groups[qid].get('bandwidth', 0):
                quality_groups[qid] = v
        # 按清晰度 ID 从高到低排序
        sorted_qids = sorted(quality_groups.keys(), reverse=True)
        # 为每个清晰度创建一个画质选项
        for qid in sorted_qids:
            v = quality_groups[qid]
            qualities.append({
                'type': 'dash',
                'video': v,
                'audio': best_a,
                'desc': f"{qid} ({v.get('width', '?')}x{v.get('height', '?')}) - DASH",
            })

    # 处理 durl 格式的直链
    if playinfo.get('durl'):
        # 按文件大小从大到小排序（文件大通常质量高）
        sorted_d = sorted(playinfo['durl'], key=lambda x: x.get('size', 0), reverse=True)
        # 为每个 durl 创建一个画质选项
        for d in sorted_d:
            qualities.append({
                'type': 'durl',
                'durl': d,
                'desc': f"{playinfo.get('video_info', {}).get('quality', '?')} (durl直链)",
            })

    # 返回所有画质选项（已按清晰度从高到低排序）
    return qualities


def choose_quality(playinfo):
    """
    列出所有可选画质，让用户选择，默认选择最高画质（第1项）。

    先调用 list_qualities 获取所有可用画质，
    然后打印出来让用户输入编号选择。
    用户直接回车或输入无效值时，默认选择第1项（最高画质）。

    参数:
        playinfo (dict):
            归一化后的播放信息字典。

    返回:
        dict or None:
            选择结果字典，格式与原 choose_best_quality 返回值一致：
            - video_url (str): 视频流的下载地址
            - audio_url (str or None): 音频流的下载地址
            - quality (str): 画质描述字符串
            如果没有找到任何可下载的媒体流，返回 None。
    """
    # 获取所有可选画质列表
    qlist = list_qualities(playinfo)
    if not qlist:
        return None

    # 打印空行和标题
    print()
    print('可选画质：')
    # 逐个打印画质选项，编号从 1 开始
    for i, q in enumerate(qlist):
        print(f"  {i + 1}. {q['desc']}")

    # 提示用户输入，默认 1（最高画质）
    choice = input(f'请选择画质（默认 1，最高画质）: ').strip()
    try:
        # 尝试将输入转为整数，转为列表索引（减1）
        idx = int(choice) - 1
        # 检查索引是否在有效范围内
        if 0 <= idx < len(qlist):
            selected = qlist[idx]
        else:
            # 超出范围，默认选第1个
            selected = qlist[0]
    except ValueError:
        # 输入不是数字，默认选第1个
        selected = qlist[0]

    # 根据选中的类型构造返回结果
    if selected['type'] == 'dash':
        # DASH 格式：从 video 和 audio 中提取地址
        v = selected['video']
        a = selected['audio']
        return {
            'video_url': v.get('baseUrl') or v.get('base_url'),
            'audio_url': a.get('baseUrl') or a.get('base_url') if a else None,
            'quality': selected['desc'],
        }
    else:
        # durl 格式：从 durl 中提取地址
        d = selected['durl']
        return {
            'video_url': d.get('url') or d.get('backup_url'),
            'audio_url': None,
            'quality': selected['desc'],
        }


def extract_title(html, playinfo):
    """
    从页面 HTML 和 playinfo 中提取视频标题，生成用于保存的文件名。

    标题提取优先级（番剧页面最优）：
        1. 番剧名 + 第X集 + 分集标题（信息最完整）
        2. 番剧名 + 第X集
        3. og:title 元标签内容（即页面的 OpenGraph 标题）
        4. <title> 标签内容（去除 "-番剧-" / "_哔哩哔哩_bilibili" 等后缀）
        5. itemProp="name" 元标签内容
        6. URL 路径（最后的兜底方案）

    参数:
        html (str):
            页面 HTML 源码，用于提取 og:title、<title> 等元信息。
        playinfo (dict):
            归一化的播放信息字典，用于提取番剧分集信息
            （supplement -> ogv_episode_info 下的 index_title、long_title 等）。

    返回:
        str:
            提取到的视频标题字符串（未经过文件名清理，需后续调用 sanitize_filename）。
    """
    # 获取补充信息中的番剧分集信息（PGC 番剧页面特有）
    sup = playinfo.get('supplement', {}) if playinfo else {}
    ep = sup.get('ogv_episode_info', {})

    # 从 HTML 的 og:title 元标签中提取番剧名称（OpenGraph 协议的标题）
    og_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
    season = og_title.group(1).strip() if og_title else ''

    # 情况1：有番剧名、集数和分集标题 -> 最完整的标题
    if season and ep.get('index_title') and ep.get('long_title'):
        return f"{season} 第{ep['index_title']}集：{ep['long_title']}"

    # 情况2：有番剧名和集数，但没有分集标题
    if season and ep.get('index_title'):
        return f"{season} 第{ep['index_title']}集"

    # 情况3：只有番剧名（普通视频页面 og:title 可能就直接是标题）
    if season:
        return season

    # 情况4：尝试从其他元标签中提取
    # 按优先级尝试多个正则表达式模式
    for pat in [r'<title>(.*?)</title>', r'<meta\s+itemProp="name"\s+content="([^"]+)"']:
        m = re.search(pat, html)
        if m:
            t = m.group(1).strip()
            # 清理常见的 Bilibili 标题后缀
            for sep in ['-番剧-', '_哔哩哔哩_bilibili', '-bilibili']:
                if sep in t:
                    t = t.split(sep)[0].strip()
            if t:
                return t

    # 情况5：最后的兜底，使用 URL 路径（替换斜杠为下划线）
    return urlparse(html).path.replace('/', '_') if html else 'video'


def download_stream(url, path, headers=None, cookies=None):
    """
    下载单个媒体流（视频或音频）到本地文件，实时显示下载进度。

    采用流式下载（stream=True）的方式，边下载边写入文件，
    避免大文件占用过多内存。同时通过 content-length 响应头
    计算下载百分比，在同一行实时更新进度条（使用 \\r 回车符实现）。

    参数:
        url (str):
            要下载的媒体文件 URL 地址。
        path (str):
            保存到本地的文件路径（包含文件名）。
        headers (dict, 可选):
            HTTP 请求头字典，用于设置 User-Agent、Referer 等。
            默认为空字典。
        cookies (dict, 可选):
            Cookie 字典（由 cookie_str_to_dict 转换而来）。
            默认为空字典。

    返回:
        None
            函数没有返回值，下载结果直接写入文件。
            如果下载失败会抛出 requests 异常。

    注意:
        - 下载进度通过 sys.stdout 的 \\r 实现在同一行刷新
        - 下载完成后会打印换行符，避免后续输出覆盖进度行
    """
    # 如果 headers 为 None，使用空字典
    headers = headers or {}
    # 如果 cookies 为 None，使用空字典
    cookies = cookies or {}

    # 记录开始下载的日志
    logger.info(f"开始下载 -> {os.path.basename(path)}")

    # 使用流式请求下载（stream=True），避免一次性加载整个文件到内存
    with requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=60) as r:
        # 如果 HTTP 状态码不是 2xx，抛出异常
        r.raise_for_status()
        # 从响应头获取文件总大小（字节数），如果没有则为 0
        total = int(r.headers.get('content-length', 0))
        # 已下载的字节数计数器
        downloaded = 0

        # 以二进制写入模式打开目标文件
        with open(path, 'wb') as f:
            # 按块迭代读取响应内容（每块 8KB）
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    # 写入当前块到文件
                    f.write(chunk)
                    # 更新已下载字节数
                    downloaded += len(chunk)
                    # 如果知道总大小，实时显示下载进度
                    if total:
                        # 计算下载百分比
                        pct = downloaded / total * 100
                        # 使用 \r 回车符回到行首，覆盖输出实现进度条效果
                        # 显示百分比和已下载/总大小（KB 单位）
                        sys.stdout.write(f"\r  进度: {pct:.1f}% ({downloaded//1024}/{total//1024} KB)")
                        # 立即刷新输出缓冲区，确保进度实时显示
                        sys.stdout.flush()

    # 下载完成后打印换行，避免后续输出覆盖进度行
    print()
    # 记录下载完成的日志
    logger.info(f"下载完成: {path}")


def have_ffmpeg():
    """
    检查当前系统是否有可用的 FFmpeg（多模式检测）。

    检测顺序（优先级从高到低）：
    1. FFMPEG_PATH 手动指定的路径（如果存在且是文件）
    2. ffmpeg-python 库是否可用且系统有 FFmpeg
    3. shutil.which('ffmpeg') 在系统 PATH 中查找

    只要任一方式可用，就返回 True。

    返回:
        bool:
            FFmpeg 可用返回 True，否则返回 False。
    """
    # 方式零：手动指定的 FFMPEG_PATH（优先级最高）
    if FFMPEG_PATH and os.path.isfile(FFMPEG_PATH):
        return True

    # 方式一：通过 ffmpeg-python 库检测
    if _HAS_FFMPEG_PY:
        try:
            ffmpeg.probe('__nonexistent__')
        except ffmpeg.Error:
            # 抛出 ffmpeg.Error 说明 FFmpeg 本身可用，只是文件不存在
            return True
        except Exception:
            # ffmpeg-python 可用但找不到 ffmpeg 可执行文件，继续尝试其他方式
            pass

    # 方式二：通过 shutil.which 在系统 PATH 中查找（最标准的方式）
    return bool(shutil.which('ffmpeg'))


def merge_av(video_path, audio_path, out_path):
    """
    使用 FFmpeg 将视频和音频文件合并为一个完整的 MP4 文件。

    支持两种调用方式（自动选择）：
    1. ffmpeg-python 库方式（默认优先，前提是库已安装且未手动指定 FFMPEG_PATH）
    2. subprocess 命令行方式（备选）：直接调用系统 ffmpeg 命令
       - 如果手动指定了 FFMPEG_PATH，也使用此方式

    Bilibili 的 DASH 格式视频和音频是分开存储的，下载完成后
    需要将它们合并成一个可播放的 MP4 文件。使用 `c='copy'` 参数
    进行无损合并（直接复制流，不重新编码，速度快）。

    参数:
        video_path (str):
            视频文件的本地路径。
        audio_path (str or None):
            音频文件的本地路径。
            如果为 None，则只对视频文件进行一次无损封装（不改内容）。
        out_path (str):
            合并后输出文件的路径。

    返回:
        bool:
            合并成功返回 True，失败返回 False。
            如果系统未安装 FFmpeg，也返回 False。
    """
    # 先检查是否有可用的 FFmpeg
    if not have_ffmpeg():
        return False

    # 记录开始合并的日志
    logger.info('ffmpeg 合并音视频...')
    # 确定 ffmpeg 可执行文件路径
    ffmpeg_exe = FFMPEG_PATH or 'ffmpeg'
    # 决定使用哪种方式：优先 ffmpeg-python，其次命令行
    # 注意：如果手动指定了 FFMPEG_PATH，则强制使用命令行方式
    use_py = _HAS_FFMPEG_PY and not FFMPEG_PATH
    try:
        if use_py:
            # ===== 方式一：使用 ffmpeg-python 库 =====
            # 创建视频输入流
            input_v = ffmpeg.input(video_path)
            if audio_path:
                # 有音频时：创建音频输入流，两个流一起输出，使用 copy 编码（无损合并）
                input_a = ffmpeg.input(audio_path)
                ffmpeg.output(input_v, input_a, out_path, c='copy').run(
                    overwrite_output=True,  # 覆盖输出文件（对应 -y）
                    quiet=True              # 静默模式，不输出 FFmpeg 日志
                )
            else:
                # 无音频时：直接复制视频流封装到输出文件
                ffmpeg.output(input_v, out_path, c='copy').run(
                    overwrite_output=True,
                    quiet=True
                )
        else:
            # ===== 方式二：使用 subprocess 命令行调用 =====
            # 构建 ffmpeg 命令
            cmd = [ffmpeg_exe, '-y', '-i', video_path]
            if audio_path:
                # 有音频时：添加第二个输入文件，复制所有流
                cmd += ['-i', audio_path, '-c', 'copy']
            else:
                # 无音频时：只复制视频流
                cmd += ['-c', 'copy']
            cmd.append(out_path)
            # 执行命令，捕获输出（静默模式）
            subprocess.run(cmd, check=True, capture_output=True)

        # 合并成功
        logger.info(f'合并完成: {out_path}')
        return True
    except Exception as e:
        # 任何异常都视为合并失败
        logger.error(f'ffmpeg 合并失败: {e}')
        return False


def main():
    """
    主函数：程序入口，协调整个下载流程。

    执行流程：
        1. 获取用户输入的视频 URL
        2. 读取 Cookie（优先从 cookie.txt 文件读取，其次让用户输入）
        3. 设置请求头（User-Agent、Referer 等）
        4. 验证 Cookie 有效性（仅日志提示，不影响流程）
        5. 请求视频页面，获取 HTML
        6. 从 HTML 中提取 playinfo JSON 数据
        7. 归一化 playinfo 数据结构
        8. 列出所有可选画质，用户选择（默认最高画质）
        9. 创建输出目录，生成安全文件名
        10. 下载视频流
        11. 下载音频流（如果有）
        12. 询问用户是否使用 FFmpeg 合并音视频
        13. 合并成功后删除原始分离文件

    返回:
        None
            函数没有返回值，执行结果通过日志和文件输出。
    """
    # ==================== 步骤1：获取 URL ====================
    url = input('请输入B站番剧URL: ').strip()
    if not url:
        logger.error('未提供 URL')
        return

    # ==================== 步骤2：获取 Cookie ====================
    cookie_string = ''
    # 优先尝试从同目录的 cookie.txt 文件中读取
    cf = os.path.join(BASE_DIR, 'cookie.txt')
    if os.path.exists(cf):
        with open(cf, 'r', encoding='utf-8') as f:
            cookie_string = f.read().strip()
    # 如果文件中没有，则让用户手动输入
    if not cookie_string:
        cookie_string = input('请粘贴 cookie（回车跳过）: ').strip()

    # ==================== 步骤3：设置目录与请求头 ====================
    # 输出目录：脚本目录下的 downloads 文件夹
    out_dir = os.path.join(BASE_DIR, 'downloads')

    # HTTP 请求头，模拟浏览器请求
    headers = {
        # 浏览器 User-Agent，伪装成 Chrome 浏览器
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        # Referer 设置为视频页面地址，B 站会校验这个字段防止盗链
        'Referer': url,
    }

    # ==================== 步骤4：验证 Cookie ====================
    # 验证 Cookie 有效性（仅输出日志，不影响后续流程）
    validate_cookie(cookie_string, headers)
    # 将 Cookie 字符串转为字典格式，供 requests 使用
    cookies = cookie_str_to_dict(cookie_string) if cookie_string else None

    # ==================== 步骤5：请求页面 HTML ====================
    try:
        r = requests.get(url, headers=headers, cookies=cookies, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        logger.error(f'请求页面失败: {e}')
        return

    # ==================== 步骤6：提取 playinfo ====================
    parsed = extract_playinfo_from_html(html)
    if not parsed:
        logger.error('未找到 playinfo JSON')
        return

    # ==================== 步骤7：归一化数据结构 ====================
    playinfo = get_playinfo_normalized(parsed)
    if not playinfo:
        logger.error('解析不到 dash/durl 信息')
        return

    # ==================== 步骤8：选择画质（用户自选，默认最高） ====================
    pick = choose_quality(playinfo)
    if not pick:
        logger.error('未找到可下载的媒体链接')
        return

    # ==================== 步骤9：准备输出目录 ====================
    # 创建输出根目录（如果不存在）
    os.makedirs(out_dir, exist_ok=True)
    # 视频子目录：存放视频文件
    video_dir = os.path.join(out_dir, 'video')
    # 音频子目录：存放音频文件
    audio_dir = os.path.join(out_dir, 'audio')
    # 创建两个子目录
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(audio_dir, exist_ok=True)

    # 提取标题并清理为安全文件名
    title = sanitize_filename(extract_title(html, playinfo))
    logger.info(f'标题: {title}')
    logger.info(f'画质: {pick["quality"]}')

    # ==================== 步骤10：下载音视频（分目录存放） ====================
    video_url = pick.get('video_url')
    audio_url = pick.get('audio_url')
    video_path = audio_path = None

    # 下载视频流到 video/ 子目录
    if video_url:
        # 从视频 URL 中提取文件扩展名，没有的话默认 .mp4
        video_ext = os.path.splitext(urlparse(video_url).path)[1] or '.mp4'
        video_path = os.path.join(video_dir, title + video_ext)
        download_stream(video_url, video_path, headers=headers, cookies=cookies)

    # 下载音频流到 audio/ 子目录（如果有独立的音频流）
    if audio_url:
        # 从音频 URL 中提取文件扩展名，没有的话默认 .m4a
        audio_ext = os.path.splitext(urlparse(audio_url).path)[1] or '.m4a'
        audio_path = os.path.join(audio_dir, title + audio_ext)
        download_stream(audio_url, audio_path, headers=headers, cookies=cookies)

    # ==================== 步骤11：合并音视频（用户确认） ====================
    if video_path and audio_path:
        # 音视频都有，询问用户是否合并，合并后的文件放在输出根目录
        merged = os.path.join(out_dir, title + '.mp4')
        print()
        do_merge = input('是否使用 FFmpeg 合并音视频为一个 MP4 文件？(Y/n): ').strip().lower()
        if do_merge in ('', 'y', 'yes'):
            # 用户确认合并，执行合并
            if merge_av(video_path, audio_path, merged):
                # 合并成功，删除原始的分离文件
                try:
                    os.remove(video_path)
                    os.remove(audio_path)
                except Exception:
                    # 删除失败不影响主流程，忽略即可
                    pass
            else:
                # 合并失败，原始文件保留在 video/ 和 audio/ 子目录
                logger.info('合并失败，原始文件保留在 video/ 和 audio/ 子目录')
        else:
            # 用户选择不合并，视频在 video/ 目录，音频在 audio/ 目录
            logger.info('跳过合并，视频在 video/ 目录，音频在 audio/ 目录')
    elif video_path:
        # 只有视频（durl 格式或无音频的情况），直接使用
        logger.info('完成: ' + video_path)


# ==================== 程序入口 ====================
# 当脚本被直接运行时（而非被 import 时），执行 main 函数
if __name__ == '__main__':
    main()
    # 程序结束前等待用户按回车，防止窗口直接关闭
    input('按回车退出...')
