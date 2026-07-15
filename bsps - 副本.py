"""
B站视频搜索下载爬虫 - 两阶段流水线架构
========================================

架构设计说明：
-------------
本脚本采用「两阶段流水线」架构设计，将下载与合并解耦，提升整体吞吐率：

【阶段一：动态进程下载】
- 根据待下载视频数量和 CPU 核心数，动态计算下载进程数（1~8 个）
- 每个进程独立负责一批视频的下载任务（音视频分片下载到本地 .tmp 目录）
- 单视频内部使用多线程分片下载（HTTP Range 请求），最大化单文件下载速度
- 动态进程策略：视频少时少开进程避免 overhead，视频多时充分利用多核

【阶段二：固定 8 进程合并】
- 所有下载完成后，统一启动合并进程池（固定 8 个工作进程）
- 合并任务包括：音视频 mux（FFmpeg）或文件重命名/移动
- 固定进程数的原因：FFmpeg 本身是 CPU 密集型，过多进程反而导致上下文切换开销
- 两阶段分离的好处：下载是 IO 密集型、合并是 CPU 密集型，资源使用模式不同，分开调度更高效

分片下载原理：
-------------
1. 先通过 HEAD 请求获取文件总大小和 accept-ranges 支持情况
2. 将文件按大小切分为 N 个分片（最多 8 片，每片至少 ~5MB）
3. 每个分片使用独立线程，通过 Range: bytes=start-end 头请求部分内容
4. 所有分片下载完成后，按顺序拼接写入最终文件
5. 不支持 Range 或文件过小时，退化为单线程流式下载

动态进程分配策略：
-----------------
- 核心原则：进程数随任务量增长，但不超过 CPU 核心数-1（留一个给系统）
-  ≤2 个视频：1 进程（单任务无需多进程 overhead）
-  ≤4 个视频：2 进程
-  ≤8 个视频：3 进程
-  ≤16 个视频：4 进程
-  >16 个视频：min(CPU-1, 8) 进程（上限 8，避免带宽压力过大）
"""

import json
import os
import re
import sys
import io
import time
import shutil
import requests
import logging
import subprocess
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote
from datetime import datetime

# 尝试导入 ffmpeg-python 库，用于 FFmpeg 可用性探测
try:
    import ffmpeg
    _HAS_FFMPEG_PY = True
except ImportError:
    ffmpeg = None
    _HAS_FFMPEG_PY = False


# ==================== 全局常量与路径配置 ====================

# 脚本所在目录的绝对路径，作为所有相对路径的基准
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# FFmpeg 可执行文件路径：优先从环境变量 FFMPEG_PATH 读取，其次从系统 PATH 查找
FFMPEG_PATH = os.environ.get('FFMPEG_PATH', '') or shutil.which('ffmpeg') or ''

# 日志文件路径：保存在脚本同目录下
LOG_PATH = os.path.join(BASE_DIR, "bilibili_spider.txt")

# 单文件下载时的最大分片线程数（控制单文件并发度，避免对服务器造成过大压力）
MAX_CHUNK_THREADS = 8

# 启用分片下载的最小文件大小：小于 2*MIN_CHUNK_SIZE 的文件不分片，直接单线程下载
MIN_CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB

# 阶段二合并阶段的固定工作进程数（FFmpeg 是 CPU 密集型，8 进程为经验值）
MERGE_WORKERS = 8

# 日志输出锁：多线程环境下防止日志行交错
_log_lock = threading.Lock()


def _setup_logger():
    """
    初始化日志记录器。

    配置日志同时输出到控制台和文件，避免重复添加 handler。

    参数:
        无

    返回:
        logging.Logger: 配置好的日志记录器实例
    """
    lg = logging.getLogger("bili_spider")
    lg.setLevel(logging.INFO)
    # 防止重复添加 handler（多次调用时幂等）
    if lg.handlers:
        return lg
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    # 控制台输出 handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    lg.addHandler(ch)
    # 文件输出 handler（追加模式，UTF-8 编码）
    fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    return lg


# 全局日志记录器实例（模块加载时初始化）
logger = _setup_logger()


def log_msg(msg, level=logging.INFO):
    """
    线程安全的日志输出函数。

    使用全局锁保证多线程/多进程环境下日志不交错。

    参数:
        msg (str): 日志消息内容
        level (int): 日志级别，默认为 INFO

    返回:
        无
    """
    with _log_lock:
        logger.log(level, msg)


def sanitize_filename(name, fallback="file"):
    """
    清理文件名，移除操作系统不允许的字符。

    Windows 系统下文件名不能包含尖括号、冒号、引号、斜杠、竖线、问号、星号以及控制字符，
    同时去除末尾的点和空格（Windows 会自动忽略）。

    参数:
        name (str): 原始文件名
        fallback (str): 清理后为空时的备用文件名，默认为 "file"

    返回:
        str: 安全可用的文件名
    """
    # 先取 basename 防止路径穿越，再去除首尾空白
    safe = os.path.basename(str(name)).strip()
    # 移除 Windows 非法字符和控制字符 \x00-\x1f
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '', safe)
    # 移除末尾的点和空格（Windows 命名规范）
    safe = re.sub(r'[.\s]+$', '', safe)
    # 若清理后为空，使用备用文件名
    return safe or fallback


def cookie_str_to_dict(cookie_string):
    """
    将 Cookie 字符串转换为字典格式。

    Cookie 字符串格式："key1=value1; key2=value2; ..."

    参数:
        cookie_string (str): 原始 Cookie 字符串

    返回:
        dict: 键值对形式的 Cookie 字典
    """
    # 按 "; " 分割，再按第一个 "=" 分割为键值对，跳过不含 "=" 的项
    return {pair.split('=', 1)[0]: pair.split('=', 1)[1] for pair in cookie_string.split('; ') if '=' in pair}


def validate_cookie(cookie_string, headers):
    """
    验证 Cookie 是否有效（是否已登录）。

    通过调用 B 站 nav 接口，检查返回的 isLogin 字段判断登录状态。

    参数:
        cookie_string (str): 待验证的 Cookie 字符串
        headers (dict): 请求头基础配置

    返回:
        bool: Cookie 有效且已登录返回 True，否则返回 False
    """
    if not cookie_string:
        log_msg("没有提供 cookie，使用匿名请求。", logging.WARNING)
        return False
    # 复制 headers 并添加 Cookie
    check_headers = dict(headers)
    check_headers['Cookie'] = cookie_string
    try:
        # 调用 B 站用户信息接口
        r = requests.get('https://api.bilibili.com/x/web-interface/nav', headers=check_headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        # 检查 isLogin 字段
        if data.get('data', {}).get('isLogin'):
            log_msg('Cookie 有效，已登录')
            return True
        log_msg('Cookie 未登录或无效', logging.WARNING)
        return False
    except Exception as e:
        log_msg(f'验证 cookie 时异常: {e}', logging.WARNING)
        return False


def search_bilibili(keyword, headers, cookies=None, max_results=50):
    """
    在 B 站搜索视频，返回 BV 号列表。

    通过请求搜索结果页面，用正则提取所有 BV 号，去重后返回。

    参数:
        keyword (str): 搜索关键词
        headers (dict): 请求头
        cookies (dict, optional): Cookie 字典，默认为 None
        max_results (int): 最大返回结果数，默认为 50

    返回:
        list: BV 号字符串列表
    """
    log_msg(f"搜索关键词: {keyword}")
    # 构造搜索 URL，关键词需要 URL 编码
    search_url = f"https://search.bilibili.com/all?keyword={quote(keyword)}"
    search_headers = dict(headers)
    search_headers['Referer'] = 'https://www.bilibili.com/'
    try:
        r = requests.get(search_url, headers=search_headers, cookies=cookies, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        log_msg(f'搜索请求失败: {e}', logging.ERROR)
        return []

    # 正则匹配 BV 号：BV + 10 位字母数字
    bv_pattern = r'//www\.bilibili\.com/video/(BV[0-9a-zA-Z]{10})'
    matches = re.findall(bv_pattern, html)
    # 去重并保持顺序（搜索结果页可能重复出现同一视频）
    seen = set()
    bv_list = []
    for bv in matches:
        if bv not in seen:
            seen.add(bv)
            bv_list.append(bv)

    # 截取前 max_results 个
    result = bv_list[:max_results]
    log_msg(f"搜索到 {len(bv_list)} 个视频，取前 {len(result)} 个")
    return result


def extract_playinfo_from_html(html):
    """
    从视频页面 HTML 中提取 playinfo JSON 数据。

    B 站视频页面会在 <script> 标签中注入 window.__playinfo__ 对象，
    包含视频/音频的播放地址、画质等信息。由于格式可能有变化（空格、等号等），
    采用多前缀匹配 + 括号深度计数的方式提取完整 JSON。

    参数:
        html (str): 视频页面 HTML 源码

    返回:
        dict or None: 解析后的 playinfo 字典，提取失败返回 None
    """
    # 可能的前缀变体（应对页面代码微小变动）
    prefixes = [
        'window.__playinfo__ =',
        'window.__playinfo__=',
        '__playinfo__ =',
    ]
    for p in prefixes:
        idx = html.find(p)
        if idx == -1:
            continue
        # 跳过前缀后的所有空白和等号
        start = idx + len(p)
        while start < len(html) and html[start] in ' \t\n\r=':
            start += 1
        # JSON 必须以 { 开头
        if start >= len(html) or html[start] != '{':
            continue
        # 括号深度计数法提取完整 JSON 对象
        depth = 0
        i = start
        in_string = False  # 是否在字符串内部（字符串中的括号不计入深度）
        escape = False     # 是否处于转义状态
        while i < len(html):
            ch = html[i]
            if escape:
                # 上一个字符是反斜杠，当前字符被转义，跳过
                escape = False
            elif ch == '\\':
                # 遇到反斜杠，标记下一个字符转义
                escape = True
            elif ch == '"':
                # 遇到引号，切换字符串状态
                in_string = not in_string
            elif not in_string:
                # 不在字符串中时才计数括号深度
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    # 深度回到 0 表示找到匹配的闭合括号
                    if depth == 0:
                        try:
                            return json.loads(html[start:i+1])
                        except json.JSONDecodeError:
                            return None
            i += 1
    return None


def get_best_quality(playinfo):
    """
    从 playinfo 中选取最佳画质的视频和音频流。

    B 站有两种播放格式：
    - DASH 格式：视频和音频分离，需要分别下载后合并
    - DURL 格式：单文件直链（旧格式，音视频在一起）

    参数:
        playinfo (dict): extract_playinfo_from_html 返回的 playinfo 数据

    返回:
        dict or None: 最佳画质信息字典，包含：
            - type: 'dash' 或 'durl'
            - video_url: 视频下载地址
            - audio_url: 音频下载地址（durl 格式为 None）
            - quality: 画质描述字符串
            - video_size: 视频文件大小（字节）
        没有可下载媒体时返回 None
    """
    data = playinfo.get('data', playinfo)
    dash = data.get('dash', {})
    vlist = dash.get('video', [])
    alist = dash.get('audio', [])

    # 没有 DASH 视频流时，尝试 DURL 格式
    if not vlist:
        durl = data.get('durl')
        if durl:
            # 选取文件最大的那个（通常画质最好）
            best = max(durl, key=lambda x: x.get('size', 0))
            return {
                'type': 'durl',
                'video_url': best.get('url') or best.get('backup_url'),
                'audio_url': None,
                'quality': 'durl直链',
                'video_size': best.get('size', 0),
            }
        return None

    # DASH 格式：按画质 id 分组，每组选带宽最高的（同画质下带宽越高画质越好）
    quality_groups = {}
    for v in vlist:
        qid = v.get('id', v.get('quality', 0))
        if qid not in quality_groups or v.get('bandwidth', 0) > quality_groups[qid].get('bandwidth', 0):
            quality_groups[qid] = v
    # 选取画质 id 最高的视频流
    best_qid = max(quality_groups.keys())
    best_v = quality_groups[best_qid]
    # 选取带宽最高的音频流
    best_a = max(alist, key=lambda x: x.get('bandwidth', 0)) if alist else None
    return {
        'type': 'dash',
        'video_url': best_v.get('baseUrl') or best_v.get('base_url'),
        'audio_url': best_a.get('baseUrl') or best_a.get('base_url') if best_a else None,
        'quality': f"{best_qid} ({best_v.get('width', '?')}x{best_v.get('height', '?')})",
        'video_size': best_v.get('size', 0),
    }


def extract_title_from_html(html):
    """
    从视频页面 HTML 中提取视频标题。

    从 <title> 标签提取后，去除 B 站后缀（如 "_哔哩哔哩_bilibili"）。

    参数:
        html (str): 视频页面 HTML 源码

    返回:
        str: 视频标题，提取失败返回 'video'
    """
    m = re.search(r'<title>(.*?)</title>', html)
    if m:
        t = m.group(1).strip()
        # 去除常见的 B 站后缀
        for sep in ['_哔哩哔哩_bilibili', '-bilibili', '哔哩哔哩']:
            if sep in t:
                t = t.split(sep)[0].strip()
        if t:
            return t
    return 'video'


def extract_pubdate(playinfo, html):
    """
    提取视频发布日期。

    优先从 playinfo 中取，取不到再从 HTML 中正则提取。

    参数:
        playinfo (dict): playinfo 数据
        html (str): 视频页面 HTML 源码

    返回:
        str or None: 格式化的日期字符串 'YYYY-MM-DD'，提取失败返回 None
    """
    data = playinfo.get('data', playinfo)
    vi = data.get('video_info', {})
    # 尝试多个可能的字段名
    pubdate = vi.get('pubdate') or vi.get('ctime') or data.get('pubdate') or data.get('ctime')
    if pubdate:
        try:
            # 时间戳转日期字符串
            dt = datetime.fromtimestamp(int(pubdate))
            return dt.strftime('%Y-%m-%d')
        except Exception:
            pass
    # playinfo 中没找到，尝试从 HTML 中正则提取
    m = re.search(r'"pubdate"\s*:\s*(\d+)', html)
    if m:
        try:
            dt = datetime.fromtimestamp(int(m.group(1)))
            return dt.strftime('%Y-%m-%d')
        except Exception:
            pass
    return None


def _get_file_size(url, headers, cookies):
    """
    获取远程文件大小，并检测是否支持断点续传（Range 请求）。

    策略：
    1. 先用 HEAD 请求，读取 content-length 和 accept-ranges 头
    2. HEAD 失败时，用 GET 请求 Range: bytes=0-0 探测（返回 206 即支持 Range）
    3. 都失败则返回 (0, False)

    参数:
        url (str): 文件 URL
        headers (dict): 请求头
        cookies (dict): Cookie 字典

    返回:
        tuple: (文件大小字节数, 是否支持 Range 请求)
    """
    try:
        # 方案一：HEAD 请求
        r = requests.head(url, headers=headers, cookies=cookies, timeout=15, allow_redirects=True)
        if r.status_code in (200, 206):
            size = int(r.headers.get('content-length', 0))
            accept_ranges = r.headers.get('accept-ranges', '').lower() == 'bytes'
            return size, accept_ranges
    except Exception:
        pass

    try:
        # 方案二：GET 请求只取第 0 字节，通过 Content-Range 头获取总大小
        r = requests.get(url, headers={**headers, 'Range': 'bytes=0-0'}, cookies=cookies, timeout=15, stream=True)
        if r.status_code == 206:
            cr = r.headers.get('content-range', '')
            # Content-Range 格式：bytes 0-0/1234567
            m = re.search(r'/(\d+)$', cr)
            if m:
                return int(m.group(1)), True
            return 0, True
    except Exception:
        pass

    return 0, False


def _download_range(url, start, end, headers, cookies, buf_list, idx, progress_dict, total_size, prefix):
    """
    下载单个分片（字节范围），结果写入共享列表的指定位置。

    这是分片下载的核心工作函数，在线程池中被调用。

    参数:
        url (str): 文件 URL
        start (int): 起始字节位置（包含）
        end (int): 结束字节位置（包含）
        headers (dict): 请求头
        cookies (dict): Cookie 字典
        buf_list (list): 共享缓冲区列表，每个元素是一个 BytesIO 对象
        idx (int): 当前分片在 buf_list 中的索引
        progress_dict (dict): 进度共享字典，含 'bytes'（已下载字节）、'lock'（锁）、'last_print'
        total_size (int): 文件总大小（用于进度百分比计算）
        prefix (str): 日志前缀（用于区分视频/音频下载）

    返回:
        int: 本分片下载的字节数

    异常:
        下载过程中的任何异常都会被抛出，由上层线程池捕获
    """
    range_headers = dict(headers)
    range_headers['Range'] = f'bytes={start}-{end}'
    downloaded = 0
    try:
        # stream=True 流式下载，避免大文件占用过多内存
        with requests.get(url, headers=range_headers, cookies=cookies, stream=True, timeout=60) as r:
            r.raise_for_status()
            buf = io.BytesIO()
            # 64KB 一块迭代读取，兼顾效率和内存
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    buf.write(chunk)
                    downloaded += len(chunk)
                    # 更新全局进度（线程安全）
                    if progress_dict is not None:
                        with progress_dict['lock']:
                            progress_dict['bytes'] += len(chunk)
                            cur = progress_dict['bytes']
                            # 每下载 1MB 才刷新一次进度输出，减少 IO 开销
                            if total_size and progress_dict.get('last_print', 0) != cur // (1024 * 1024):
                                progress_dict['last_print'] = cur // (1024 * 1024)
                                pct = cur / total_size * 100
                                sys.stdout.write(f"\r  {prefix}进度: {pct:.1f}% ({cur//1024//1024}/{total_size//1024//1024} MB)")
                                sys.stdout.flush()
            # 将缓冲区存入共享列表的对应位置
            buf_list[idx] = buf
        return downloaded
    except Exception as e:
        # 标记失败位置为 None，上层可据此判断哪些分片失败
        buf_list[idx] = None
        raise e


def download_file_chunked(url, out_path, headers=None, cookies=None, prefix=""):
    """
    分片下载文件到本地。

    【分片下载原理】：
    1. 探测文件大小和 Range 支持
    2. 不支持 Range 或文件过小 → 单线程流式下载
    3. 支持 Range → 计算分片数（按每片 ~5MB，最多 8 片）
    4. 多线程并行下载各分片到内存缓冲区
    5. 按顺序拼接写入最终文件

    参数:
        url (str): 文件下载地址
        out_path (str): 输出文件路径
        headers (dict, optional): 请求头，默认为 None
        cookies (dict, optional): Cookie 字典，默认为 None
        prefix (str): 日志/进度前缀，默认为空

    返回:
        int: 下载的总字节数
    """
    headers = headers or {}
    cookies = cookies or {}
    fname = os.path.basename(out_path)
    log_msg(f"{prefix}开始下载 -> {fname} (多线程分片)")

    # 第一步：获取文件大小和 Range 支持情况
    total_size, supports_range = _get_file_size(url, headers, cookies)

    # 不支持断点续传 或 文件太小（不足 2 个最小分片）→ 退化为单线程下载
    if not supports_range or total_size < MIN_CHUNK_SIZE * 2:
        downloaded = 0
        with requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        # 有总大小时显示进度
                        if total_size:
                            pct = downloaded / total_size * 100
                            sys.stdout.write(f"\r  {prefix}进度: {pct:.1f}% ({downloaded//1024//1024}/{total_size//1024//1024} MB)")
                            sys.stdout.flush()
        print()
        log_msg(f"{prefix}下载完成: {fname} ({downloaded//1024//1024} MB)")
        return downloaded

    # 第二步：计算分片数和分片范围
    # 分片数 = min(最大线程数, max(至少2片, 文件大小 // 每片5MB))
    # 每片约 5MB 是经验值：片数太少加速不明显，片数太多 overhead 增加
    num_chunks = min(MAX_CHUNK_THREADS, max(2, total_size // (5 * 1024 * 1024)))
    chunk_size = total_size // num_chunks
    chunks = []
    for i in range(num_chunks):
        start = i * chunk_size
        # 最后一片包含剩余所有字节（整除可能有余数）
        end = start + chunk_size - 1 if i < num_chunks - 1 else total_size - 1
        chunks.append((start, end))

    log_msg(f"{prefix}分片数: {num_chunks}, 单分片大小: ~{chunk_size//1024//1024} MB")

    # 第三步：多线程并行下载各分片
    buf_list = [None] * num_chunks
    # 进度共享字典：bytes 累计已下载字节，lock 线程锁，last_print 控制输出频率
    progress_dict = {'bytes': 0, 'lock': threading.Lock(), 'last_print': -1}

    with ThreadPoolExecutor(max_workers=num_chunks) as executor:
        futures = []
        for i, (start, end) in enumerate(chunks):
            futures.append(executor.submit(
                _download_range, url, start, end, headers, cookies, buf_list, i,
                progress_dict, total_size, prefix
            ))
        # 等待所有分片完成，任意分片失败会在此抛出异常
        for f in as_completed(futures):
            f.result()

    print()

    # 第四步：按分片顺序拼接写入最终文件
    with open(out_path, 'wb') as f:
        for b in buf_list:
            if b:
                b.seek(0)
                f.write(b.read())

    log_msg(f"{prefix}下载完成: {fname} ({total_size//1024//1024} MB)")
    return total_size


def have_ffmpeg():
    """
    检测系统中是否有可用的 FFmpeg。

    检测顺序：
    1. FFMPEG_PATH 环境变量指定的路径
    2. ffmpeg-python 库是否可用（通过 probe 命令探测）
    3. 系统 PATH 中是否有 ffmpeg 命令

    参数:
        无

    返回:
        bool: 有可用 FFmpeg 返回 True，否则返回 False
    """
    # 方式一：环境变量指定路径
    if FFMPEG_PATH and os.path.isfile(FFMPEG_PATH):
        return True
    # 方式二：ffmpeg-python 库可用（通过 probe 一个不存在的文件，能报错说明 ffprobe 存在）
    if _HAS_FFMPEG_PY:
        try:
            ffmpeg.probe('__nonexistent__')
        except ffmpeg.Error:
            # 抛出 ffmpeg.Error 说明 ffprobe 存在且运行了（只是文件不存在）
            return True
        except Exception:
            pass
    # 方式三：系统 PATH 查找
    return bool(shutil.which('ffmpeg'))


def merge_av_file(video_path, audio_path, out_path, cleanup=True):
    """
    使用 FFmpeg 合并音视频文件（DASH 格式下载的音视频是分离的）。

    使用 -c copy 直接复制流，不重新编码，速度快且无损画质。

    参数:
        video_path (str): 视频文件路径
        audio_path (str): 音频文件路径（为 None 时只处理视频）
        out_path (str): 输出文件路径
        cleanup (bool): 合并成功后是否删除原始音视频文件，默认为 True

    返回:
        bool: 合并成功返回 True，失败返回 False
    """
    if not have_ffmpeg():
        return False
    ffmpeg_exe = FFMPEG_PATH or 'ffmpeg'
    try:
        # -y: 覆盖输出文件不询问
        cmd = [ffmpeg_exe, '-y', '-i', video_path]
        if audio_path:
            # 有音频时，两个输入，流复制
            cmd += ['-i', audio_path, '-c', 'copy']
        else:
            # 无音频时，单纯流复制（相当于格式转换）
            cmd += ['-c', 'copy']
        cmd.append(out_path)
        # capture_output=True 捕获输出避免污染控制台，timeout 10 分钟应对大文件
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        # 合并成功后清理临时文件
        if cleanup:
            try:
                os.remove(video_path)
                if audio_path:
                    os.remove(audio_path)
            except Exception:
                pass
        return True
    except Exception:
        return False


def merge_worker_task(task):
    """
    合并阶段的工作任务函数（在子进程中执行）。

    处理逻辑：
    1. 检查视频文件是否存在
    2. 有音频且 FFmpeg 可用 → 调用 FFmpeg 合并
    3. 否则 → 直接将视频文件移动/重命名为最终路径

    参数:
        task (dict): 任务字典，包含：
            - video_path: 视频文件路径
            - audio_path: 音频文件路径（可选）
            - out_path: 输出文件路径
            - bv: BV 号
            - title: 视频标题

    返回:
        dict: 结果字典，包含 success、path、error 等字段
    """
    video_path = task.get('video_path')
    audio_path = task.get('audio_path')
    out_path = task.get('out_path')
    bv = task.get('bv')
    title = task.get('title')

    # 视频文件不存在，直接失败
    if not video_path or not os.path.exists(video_path):
        return {'bv': bv, 'title': title, 'success': False, 'error': '视频文件不存在'}

    # 音频文件不存在时，忽略音频
    if audio_path and not os.path.exists(audio_path):
        audio_path = None

    # 有音频且 FFmpeg 可用 → 音视频合并
    if audio_path and have_ffmpeg():
        ok = merge_av_file(video_path, audio_path, out_path, cleanup=True)
        if ok:
            return {'bv': bv, 'title': title, 'success': True, 'path': out_path}
        else:
            # 合并失败但视频还在，也算成功（只是没合并）
            return {'bv': bv, 'title': title, 'success': True, 'path': video_path, 'merged': False}
    else:
        # 无音频或无 FFmpeg → 直接移动/重命名视频文件
        if os.path.dirname(video_path) != os.path.dirname(out_path):
            # 不同目录用 move（可跨磁盘）
            shutil.move(video_path, out_path)
        else:
            # 同目录优先用 rename（原子操作，更快）
            try:
                os.rename(video_path, out_path)
            except Exception:
                # rename 失败（如跨磁盘）时退回 move
                shutil.move(video_path, out_path)
        return {'bv': bv, 'title': title, 'success': True, 'path': out_path}


def download_single_video(bv, out_dir, headers_base, cookie_string, worker_id=0):
    """
    下载单个视频的完整流程（在下载子进程中调用）。

    流程：
    1. 请求视频页面 HTML
    2. 提取 playinfo、标题、发布日期
    3. 选取最佳画质
    4. 下载视频（和音频，DASH 格式时音视频并行下载）
    5. 返回结果和合并任务

    参数:
        bv (str): 视频 BV 号
        out_dir (str): 输出根目录
        headers_base (dict): 基础请求头
        cookie_string (str): Cookie 字符串
        worker_id (int): 工作进程编号，用于日志前缀

    返回:
        dict: 结果字典，包含 success、bv、title、merge_task 等字段
    """
    prefix = f"[下载{worker_id}] "
    video_url = f"https://www.bilibili.com/video/{bv}"
    headers = dict(headers_base)
    headers['Referer'] = video_url
    cookies = cookie_str_to_dict(cookie_string) if cookie_string else None

    log_msg(f"{prefix}处理视频: {bv}")

    # 第一步：请求视频页面
    try:
        time.sleep(1)  # 简单的请求间隔，避免请求过快被风控
        r = requests.get(video_url, headers=headers, cookies=cookies, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        log_msg(f'{prefix}请求页面失败 {bv}: {e}', logging.ERROR)
        return {'bv': bv, 'success': False, 'error': str(e), 'merge_task': None}

    # 第二步：提取 playinfo
    playinfo = extract_playinfo_from_html(html)
    if not playinfo:
        log_msg(f'{prefix}未找到 playinfo JSON: {bv}', logging.ERROR)
        return {'bv': bv, 'success': False, 'error': '未找到 playinfo', 'merge_task': None}

    # 第三步：选取最佳画质
    best = get_best_quality(playinfo)
    if not best:
        log_msg(f'{prefix}未找到可下载的媒体: {bv}', logging.ERROR)
        return {'bv': bv, 'success': False, 'error': '无可下载媒体', 'merge_task': None}

    # 第四步：提取标题和发布日期
    title = sanitize_filename(extract_title_from_html(html))
    pubdate = extract_pubdate(playinfo, html)
    if pubdate:
        file_title = f"[{pubdate}] {title}"
    else:
        file_title = title
    log_msg(f"{prefix}标题: {title}")
    if pubdate:
        log_msg(f"{prefix}发布时间: {pubdate}")
    log_msg(f"{prefix}画质: {best['quality']}")

    video_url_dl = best.get('video_url')
    audio_url_dl = best.get('audio_url')
    video_path = audio_path = None

    # 临时目录：下载的分片文件先存在 .tmp 目录，合并完成后移到输出目录
    tmp_dir = os.path.join(out_dir, '.tmp')
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        if video_url_dl and audio_url_dl:
            # DASH 格式：音视频分离，并行下载提高速度
            video_path = os.path.join(tmp_dir, f"{file_title}_video.m4s")
            audio_path = os.path.join(tmp_dir, f"{file_title}_audio.m4a")

            # 2 线程：视频、音频各一个线程并行下载
            with ThreadPoolExecutor(max_workers=2) as pool:
                fv = pool.submit(
                    download_file_chunked, video_url_dl, video_path, headers, cookies, f"{prefix}[视频] "
                )
                fa = pool.submit(
                    download_file_chunked, audio_url_dl, audio_path, headers, cookies, f"{prefix}[音频] "
                )
                fv.result()
                fa.result()

        elif video_url_dl:
            # 只有视频（DURL 格式），直接下载
            video_path = os.path.join(tmp_dir, f"{file_title}_video.mp4")
            download_file_chunked(
                video_url_dl, video_path, headers, cookies, f"{prefix}[视频] "
            )

    except Exception as e:
        log_msg(f'{prefix}下载失败 {bv}: {e}', logging.ERROR)
        return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': False,
                'error': f'下载失败: {e}', 'merge_task': None}

    # 构造合并任务（阶段二使用）
    merged_path = os.path.join(out_dir, file_title + '.mp4')

    merge_task = {
        'video_path': video_path,
        'audio_path': audio_path,
        'out_path': merged_path,
        'bv': bv,
        'title': title,
    }

    log_msg(f"{prefix}下载完成，待合并: {file_title}")

    return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': True,
            'path': merged_path, 'quality': best['quality'], 'merge_task': merge_task}


def download_worker_task(worker_id, bv_batch, out_dir, headers, cookie_string):
    """
    下载阶段的工作进程入口函数。

    每个下载进程负责一批视频的顺序下载，视频之间间隔 2 秒。

    参数:
        worker_id (int): 工作进程编号
        bv_batch (list): 本进程负责的 BV 号列表
        out_dir (str): 输出根目录
        headers (dict): 请求头
        cookie_string (str): Cookie 字符串

    返回:
        list: 每个视频的下载结果字典列表
    """
    log_msg(f"===== 下载进程 {worker_id} 启动，负责 {len(bv_batch)} 个视频 =====")
    results = []
    for i, bv in enumerate(bv_batch):
        log_msg(f"[下载{worker_id}] 第 {i+1}/{len(bv_batch)} 个视频")
        result = download_single_video(bv, out_dir, headers, cookie_string, worker_id)
        results.append(result)
        # 视频之间间隔 2 秒，降低被风控概率
        if i < len(bv_batch) - 1:
            time.sleep(2)

    success_count = sum(1 for r in results if r.get('success'))
    log_msg(f"===== 下载进程 {worker_id} 完成，成功 {success_count}/{len(results)} =====")
    return results


def calc_dynamic_workers(total_count):
    """
    动态计算下载阶段的进程数。

    【动态进程分配策略】：
    - 基准：CPU 核心数 - 1（留一个核心给系统和主进程）
    - 随任务量阶梯增长，避免小任务开多进程 overhead 大于收益
    - 上限 8 进程（过多进程对 B 站服务器压力大，且带宽可能成为瓶颈）

    阶梯设计：
      ≤2 个视频 → 1 进程
      ≤4 个视频 → 2 进程
      ≤8 个视频 → 3 进程
      ≤16 个视频 → 4 进程
      >16 个视频 → min(CPU-1, 8) 进程

    参数:
        total_count (int): 待下载视频总数

    返回:
        int: 下载进程数
    """
    cpu_count = multiprocessing.cpu_count()
    # 最大进程数 = CPU 核心数 - 1（留一个给系统）
    max_workers = max(1, cpu_count - 1)

    if total_count <= 2:
        return 1
    elif total_count <= 4:
        return min(2, max_workers)
    elif total_count <= 8:
        return min(3, max_workers)
    elif total_count <= 16:
        return min(4, max_workers)
    else:
        return min(max_workers, 8)


def main():
    """
    主函数：两阶段流水线的调度中心。

    【两阶段流水线架构】：
    阶段一（下载）：动态进程数，IO 密集型，多进程 + 内部多线程分片
    阶段二（合并）：固定 8 进程，CPU 密集型（FFmpeg 编码）

    为什么分两阶段？
    - 下载是 IO 密集型：主要等待网络响应，多开进程/线程能提升吞吐
    - 合并是 CPU 密集型：FFmpeg 吃 CPU，进程数应接近 CPU 核心数
    - 两阶段分离让资源使用更可控，不会出现下载和合并抢资源的情况
    - 所有下载完成后再合并，便于错误处理和进度统计

    参数:
        无

    返回:
        无
    """
    # ---------- 交互输入 ----------
    keyword = input('请输入搜索关键词: ').strip()
    if not keyword:
        log_msg('未提供关键词', logging.ERROR)
        return

    count_str = input('请输入下载数量（默认5）: ').strip()
    try:
        count = int(count_str) if count_str else 5
    except ValueError:
        count = 5
    if count <= 0:
        count = 5

    # 读取 Cookie：优先从 cookie.txt 文件读取，其次用户输入
    cookie_string = ''
    cf = os.path.join(BASE_DIR, 'cookie.txt')
    if os.path.exists(cf):
        with open(cf, 'r', encoding='utf-8') as f:
            cookie_string = f.read().strip()
    if not cookie_string:
        cookie_string = input('请粘贴 cookie（回车跳过）: ').strip()

    # ---------- 初始化目录 ----------
    out_dir = os.path.join(BASE_DIR, 'output')
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, 'video'), exist_ok=True)
    os.makedirs(os.path.join(out_dir, 'audio'), exist_ok=True)
    os.makedirs(os.path.join(out_dir, '.tmp'), exist_ok=True)

    # 请求头：伪装成浏览器
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    }

    # ---------- 任务开始 ----------
    log_msg("===== B站搜索爬虫任务开始 =====")
    log_msg(f"搜索关键词: {keyword}")
    log_msg(f"下载数量: {count}")

    # 验证 Cookie 有效性
    validate_cookie(cookie_string, headers)

    # 搜索视频
    bv_list = search_bilibili(keyword, headers, cookie_str_to_dict(cookie_string) if cookie_string else None, count)
    if not bv_list:
        log_msg('未搜索到视频', logging.ERROR)
        return

    actual_count = min(count, len(bv_list))
    bv_list = bv_list[:actual_count]
    log_msg(f"将下载 {actual_count} 个视频")

    # 检查 FFmpeg 可用性
    if have_ffmpeg():
        log_msg('FFmpeg 可用，下载完成后自动合并音视频')
    else:
        log_msg('未检测到 FFmpeg，将只下载不合并', logging.WARNING)

    # ==================== 阶段一：下载 ====================
    # 动态计算下载进程数
    num_download_workers = calc_dynamic_workers(actual_count)
    # 向上取整计算每个进程的视频数：(n + m - 1) // m
    per_worker = max(1, (actual_count + num_download_workers - 1) // num_download_workers)
    log_msg(f"阶段一[下载]: 动态分配 {num_download_workers} 个下载进程，每进程约 {per_worker} 个视频 (CPU核心: {multiprocessing.cpu_count()})")

    # 将 BV 列表切分为批次
    batches = []
    for i in range(num_download_workers):
        batch = bv_list[i * per_worker:(i + 1) * per_worker]
        if batch:
            batches.append((i, batch))

    start_time = time.time()

    log_msg("===== 阶段一：开始下载 =====")

    # 单进程时直接调用（避免多进程 overhead，也便于调试）
    if num_download_workers == 1:
        all_results = download_worker_task(0, bv_list, out_dir, headers, cookie_string)
    else:
        # 多进程下载池
        pool = multiprocessing.Pool(processes=num_download_workers)
        async_results = []
        for wid, batch in batches:
            async_results.append(pool.apply_async(download_worker_task, args=(wid, batch, out_dir, headers, cookie_string)))
        pool.close()
        pool.join()
        # 收集所有进程的结果
        all_results = []
        for ar in async_results:
            all_results.extend(ar.get())

    download_elapsed = time.time() - start_time
    log_msg(f"===== 阶段一完成：下载耗时 {download_elapsed:.1f} 秒 =====")

    # 统计下载结果
    download_success = [r for r in all_results if r.get('success')]
    download_fail = [r for r in all_results if not r.get('success')]
    log_msg(f"下载成功: {len(download_success)} 个, 下载失败: {len(download_fail)} 个")

    # 收集合并任务（只有下载成功的视频才有合并任务）
    merge_tasks = [r['merge_task'] for r in all_results if r.get('success') and r.get('merge_task')]

    # ==================== 阶段二：合并 ====================
    if not merge_tasks:
        log_msg('没有需要合并的任务')
    else:
        # 固定 MERGE_WORKERS 个合并进程（FFmpeg 是 CPU 密集型，经验值 8）
        num_merge_workers = min(MERGE_WORKERS, len(merge_tasks))
        log_msg(f"阶段二[合并]: 启动 {num_merge_workers} 个合并进程（共 {len(merge_tasks)} 个合并任务）")

        log_msg("===== 阶段二：开始合并 =====")
        merge_start = time.time()

        # 单任务直接处理
        if num_merge_workers == 1:
            merge_results = [merge_worker_task(t) for t in merge_tasks]
        else:
            # 多进程合并池
            merge_pool = multiprocessing.Pool(processes=num_merge_workers)
            merge_results = merge_pool.map(merge_worker_task, merge_tasks)
            merge_pool.close()
            merge_pool.join()

        merge_elapsed = time.time() - merge_start
        log_msg(f"===== 阶段二完成：合并耗时 {merge_elapsed:.1f} 秒 =====")

        # 统计合并结果，更新到下载成功列表中
        merge_success = [r for r in merge_results if r.get('success') and r.get('path') and os.path.exists(r.get('path'))]
        for r in download_success:
            for mr in merge_results:
                if mr.get('bv') == r.get('bv'):
                    r['merged'] = mr.get('success', False) and not mr.get('merged', False) == False
                    if not mr.get('success'):
                        r['merge_error'] = mr.get('error')
                    break

    # 清理空的临时目录
    tmp_dir = os.path.join(out_dir, '.tmp')
    try:
        if os.path.exists(tmp_dir) and not os.listdir(tmp_dir):
            os.rmdir(tmp_dir)
    except Exception:
        pass

    # ---------- 结果汇总 ----------
    elapsed = time.time() - start_time

    success_list = [r for r in all_results if r.get('success')]
    fail_list = [r for r in all_results if not r.get('success')]

    print()
    log_msg("===== 任务全部执行完毕 =====")
    log_msg(f"总耗时: {elapsed:.1f} 秒 (下载: {download_elapsed:.1f}s)")
    log_msg(f"成功: {len(success_list)} 个")
    log_msg(f"失败: {len(fail_list)} 个")

    if success_list:
        log_msg("成功列表:")
        for r in success_list:
            log_msg(f"  ✓ {r.get('bv', '?')} - {r.get('title', '?')} [{r.get('quality', '?')}]")

    if fail_list:
        log_msg("失败列表:")
        for r in fail_list:
            log_msg(f"  ✗ {r.get('bv', '?')} - {r.get('error', '未知错误')}")

    log_msg(f"输出目录: {out_dir}")


if __name__ == '__main__':
    # Windows 打包 exe 时需要 freeze_support，否则多进程会出错
    multiprocessing.freeze_support()
    main()
    input('按回车退出...')
