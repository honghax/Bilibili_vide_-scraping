# -*- coding: utf-8 -*-
"""
B站搜索批量下载爬虫（详细注释版）
====================================

功能概述：
    1. 通过关键词在 B 站搜索视频，提取 BV 号
    2. 使用多进程并发下载多个视频（每个进程负责 2 个视频）
    3. 自动选择最高画质，下载视频和音频流
    4. 自动调用 FFmpeg 合并音视频为完整 MP4
    5. 完整的日志输出（控制台 + 文件）
    6. 任务结束时输出详细总结（成功/失败列表、总耗时）

使用方式：
    python "bsps - 副本.py"
    然后根据提示输入搜索关键词、下载数量等

注意事项：
    - 请遵守 B 站用户协议和相关法律法规
    - 仅供个人学习研究使用，请勿用于商业用途
    - 请勿大规模批量下载，避免对服务器造成压力
"""

# ==================== 标准库导入 ====================
import json                     # JSON 数据解析与序列化
import os                       # 操作系统接口（文件路径、目录操作等）
import re                       # 正则表达式，用于字符串匹配与提取
import sys                      # 系统相关参数与函数（用于刷新输出等）
import time                     # 时间相关函数（用于延时、计时）
import shutil                   # 高阶文件操作（用于查找 ffmpeg 可执行文件路径）
import requests                 # HTTP 请求库，用于发送网络请求
import logging                  # 日志记录模块
import subprocess               # 子进程管理（备用命令行调用 FFmpeg）
import multiprocessing          # 多进程模块，用于并发下载
from urllib.parse import urlparse, quote  # URL 解析工具 + URL 编码函数
from datetime import datetime   # 日期时间处理，用于格式化发布时间


# ==================== 第三方库可选导入 ====================
# 尝试导入 ffmpeg-python 库，如果没安装则设置标志位为 False
# 这样即使没装这个库，脚本也能正常运行（只是不能用库方式合并）
try:
    import ffmpeg
    _HAS_FFMPEG_PY = True
except ImportError:
    ffmpeg = None
    _HAS_FFMPEG_PY = False


# ==================== 全局路径配置 ====================
# 脚本所在目录的绝对路径，作为所有相对路径的基准
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==================== FFmpeg 路径配置 ====================
# FFmpeg 可执行文件路径，优先级：
# 1. 环境变量 FFMPEG_PATH（如果设置了的话）
# 2. shutil.which('ffmpeg') 在系统 PATH 中查找
# 3. 空字符串（表示未找到，后续合并功能将被跳过）
# 如需手动指定，直接修改下一行：例如 FFMPEG_PATH = r'C:\ffmpeg\bin\ffmpeg.exe'
FFMPEG_PATH = os.environ.get('FFMPEG_PATH', '') or shutil.which('ffmpeg') or ''

# ==================== 日志文件路径 ====================
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


# ==================== 工具函数 ====================

def sanitize_filename(name, fallback="file"):
    """
    清理文件名中的非法字符，使其可以安全地用于文件系统。

    Windows 文件名不允许包含以下字符：< > : " / \\ | ? *
    同时也会去掉控制字符（ASCII 0-31）和末尾的点号、空格。

    参数:
        name (str):
            原始文件名。
        fallback (str, 可选):
            清理后如果为空，使用的默认文件名。默认为 "file"。

    返回:
        str:
            清理后的安全文件名。
    """
    # 取文件名部分（去掉路径），并去除首尾空白
    safe = os.path.basename(str(name)).strip()
    # 移除所有非法字符和控制字符
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '', safe)
    # 移除末尾的点号和空格（Windows 文件名末尾不允许有点和空格）
    safe = re.sub(r'[.\s]+$', '', safe)
    # 如果清理后为空，返回默认值
    return safe or fallback


def cookie_str_to_dict(cookie_string):
    """
    将 Cookie 字符串转换为字典格式，方便 requests 库使用。

    Cookie 字符串格式：
        "key1=value1; key2=value2; key3=value3"

    参数:
        cookie_string (str):
            原始 Cookie 字符串。

    返回:
        dict:
            键值对形式的 Cookie 字典。
    """
    # 按 "; " 分割，然后对每一段按 "=" 分割成键值对
    # split('=', 1) 表示只分割一次（value 中可能包含等号）
    return {pair.split('=', 1)[0]: pair.split('=', 1)[1] for pair in cookie_string.split('; ') if '=' in pair}


def validate_cookie(cookie_string, headers):
    """
    验证 Cookie 是否有效（是否处于登录状态）。

    通过请求 B 站的用户信息接口 /x/web-interface/nav 来判断登录状态。
    如果返回的 data.isLogin 为 True，说明 Cookie 有效。

    参数:
        cookie_string (str):
            待验证的 Cookie 字符串。
        headers (dict):
            请求头基础配置。

    返回:
        bool:
            Cookie 有效且已登录返回 True，否则返回 False。
    """
    # 如果没有提供 Cookie，直接返回 False
    if not cookie_string:
        logger.warning("没有提供 cookie，使用匿名请求。")
        return False

    # 复制请求头，避免修改原字典
    check_headers = dict(headers)
    # 在请求头中添加 Cookie
    check_headers['Cookie'] = cookie_string

    try:
        # 请求 B 站用户信息接口
        r = requests.get('https://api.bilibili.com/x/web-interface/nav', headers=check_headers, timeout=10)
        r.raise_for_status()  # 如果 HTTP 状态码不是 2xx，抛出异常
        data = r.json()

        # 判断是否登录
        if data.get('data', {}).get('isLogin'):
            logger.info('Cookie 有效，已登录')
            return True

        logger.warning('Cookie 未登录或无效')
        return False
    except Exception as e:
        # 网络异常或其他错误
        logger.warning(f'验证 cookie 时异常: {e}')
        return False


# ==================== 搜索模块 ====================

def search_bilibili(keyword, headers, cookies=None, max_results=50):
    """
    在 B 站搜索视频，提取搜索结果中的 BV 号列表。

    实现原理：
        1. 请求 B 站搜索页面（https://search.bilibili.com/all?keyword=xxx）
        2. 从返回的 HTML 中用正则匹配所有视频链接
        3. 提取 BV 号并去重
        4. 返回前 N 个结果

    参数:
        keyword (str):
            搜索关键词。
        headers (dict):
            基础请求头（包含 User-Agent 等）。
        cookies (dict, 可选):
            Cookie 字典，用于登录状态搜索。
        max_results (int, 可选):
            最多返回多少个结果。默认为 50。

    返回:
        list:
            BV 号字符串列表，按搜索结果顺序排列，已去重。
    """
    logger.info(f"搜索关键词: {keyword}")

    # 构建搜索页面 URL，关键词需要 URL 编码
    search_url = f"https://search.bilibili.com/all?keyword={quote(keyword)}"

    # 构造搜索请求头，设置正确的 Referer（绕过防盗链检测）
    search_headers = dict(headers)
    search_headers['Referer'] = 'https://www.bilibili.com/'

    try:
        # 发送搜索请求
        r = requests.get(search_url, headers=search_headers, cookies=cookies, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        logger.error(f'搜索请求失败: {e}')
        return []

    # 用正则表达式从 HTML 中提取所有 BV 号
    # 匹配模式：//www.bilibili.com/video/BVxxxxxxxxxx
    bv_pattern = r'//www\.bilibili\.com/video/(BV[0-9a-zA-Z]{10})'
    matches = re.findall(bv_pattern, html)

    # 去重（保持原有顺序）
    seen = set()          # 用集合记录已经出现过的 BV 号
    bv_list = []          # 最终的去重列表
    for bv in matches:
        if bv not in seen:
            seen.add(bv)
            bv_list.append(bv)

    # 截取前 max_results 个结果
    result = bv_list[:max_results]
    logger.info(f"搜索到 {len(bv_list)} 个视频，取前 {len(result)} 个")
    return result


# ==================== 视频信息解析模块 ====================

def extract_playinfo_from_html(html):
    """
    从视频页面 HTML 中提取播放信息 JSON（playinfo）。

    B 站的视频播放信息（包含视频流、音频流地址等）是嵌入在页面的
    JavaScript 代码中的，变量名通常是 `window.__playinfo__`。

    由于 JSON 是嵌套的（有很多层大括号），普通正则无法准确匹配，
    这里使用「括号深度计数法」来精确提取完整的 JSON 对象。

    参数:
        html (str):
            视频页面的 HTML 源代码。

    返回:
        dict or None:
            解析成功返回 playinfo 字典，失败返回 None。
    """
    # 可能的 playinfo 前缀（B 站不同页面可能有不同的变量名格式）
    prefixes = [
        'window.__playinfo__ =',   # 带空格的格式
        'window.__playinfo__=',    # 不带空格的格式
        '__playinfo__ =',          # 省略 window. 的格式
    ]

    # 尝试每种前缀
    for p in prefixes:
        idx = html.find(p)
        if idx == -1:
            continue  # 没找到这个前缀，试下一个

        # 跳过前缀，找到 JSON 开始的位置
        start = idx + len(p)
        # 跳过可能的空白字符和等号（虽然前缀里已经有等号了，但保险起见）
        while start < len(html) and html[start] in ' \t\n\r=':
            start += 1

        # JSON 必须以 { 开头
        if start >= len(html) or html[start] != '{':
            continue

        # ========== 括号深度计数法提取完整 JSON ==========
        depth = 0           # 当前大括号深度
        i = start           # 当前扫描位置
        in_string = False   # 是否在字符串内部（避免把字符串里的大括号算进去）
        escape = False      # 前一个字符是否是转义符 \

        while i < len(html):
            ch = html[i]

            if escape:
                # 前一个字符是转义符，这个字符被转义了，跳过
                escape = False
            elif ch == '\\':
                # 遇到转义符，标记下一个字符被转义
                escape = True
            elif ch == '"':
                # 遇到双引号，切换字符串状态
                in_string = not in_string
            elif not in_string:
                # 不在字符串内部时才计数大括号
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    # 深度回到 0，说明找到了完整的 JSON 对象
                    if depth == 0:
                        try:
                            # 尝试解析 JSON
                            return json.loads(html[start:i+1])
                        except json.JSONDecodeError:
                            # JSON 解析失败，可能不是合法的 playinfo
                            return None

            i += 1

    # 所有前缀都没找到
    return None


def get_best_quality(playinfo):
    """
    从 playinfo 中选择最高画质的视频流和最高码率的音频流。

    处理逻辑：
    1. 优先处理 DASH 格式（视频音频分离）：
       - 按清晰度 ID 分组，同一清晰度可能有多个编码（H.264/H.265/AV1）
       - 每组中保留带宽最高的那个视频流
       - 选择清晰度 ID 最大的那组（最高画质）
       - 音频选择带宽最高的
    2. 如果没有 DASH，尝试 DURL 格式（直接视频链接）
       - 选择文件体积最大的那个

    参数:
        playinfo (dict):
            从页面提取的播放信息字典。

    返回:
        dict or None:
            包含视频地址、音频地址、画质描述的字典。
            结构：{'type': 'dash'/'durl', 'video_url': ..., 'audio_url': ..., 'quality': ...}
            如果没有可用的媒体流，返回 None。
    """
    # 兼容不同的数据结构：有的直接在根目录，有的在 data 字段里
    data = playinfo.get('data', playinfo)

    dash = data.get('dash', {})       # DASH 格式数据
    vlist = dash.get('video', [])      # 视频流列表
    alist = dash.get('audio', [])      # 音频流列表

    # 如果没有 DASH 视频流，尝试 DURL 格式（传统的直链）
    if not vlist:
        durl = data.get('durl')
        if durl:
            # DURL 格式通常是完整的视频文件，选体积最大的
            best = max(durl, key=lambda x: x.get('size', 0))
            return {
                'type': 'durl',
                'video_url': best.get('url') or best.get('backup_url'),
                'audio_url': None,
                'quality': 'durl直链',
            }
        return None

    # ========== DASH 格式：选择最高画质 ==========
    # 按清晰度 ID 分组，同一清晰度可能有多个编码格式
    quality_groups = {}
    for v in vlist:
        qid = v.get('id', v.get('quality', 0))  # 清晰度 ID
        # 同清晰度中保留带宽最高的（通常质量更好）
        if qid not in quality_groups or v.get('bandwidth', 0) > quality_groups[qid].get('bandwidth', 0):
            quality_groups[qid] = v

    # 选择最高清晰度（ID 最大的）
    best_qid = max(quality_groups.keys())
    best_v = quality_groups[best_qid]

    # 选择最高码率的音频
    best_a = max(alist, key=lambda x: x.get('bandwidth', 0)) if alist else None

    return {
        'type': 'dash',
        'video_url': best_v.get('baseUrl') or best_v.get('base_url'),
        'audio_url': best_a.get('baseUrl') or best_a.get('base_url') if best_a else None,
        'quality': f"{best_qid} ({best_v.get('width', '?')}x{best_v.get('height', '?')})",
    }


def extract_title_from_html(html):
    """
    从视频页面 HTML 中提取视频标题。

    从 <title> 标签中提取，然后去掉 B 站的后缀（如 "_哔哩哔哩_bilibili"）。

    参数:
        html (str):
            视频页面 HTML 源代码。

    返回:
        str:
            提取到的视频标题，如果提取失败返回 "video"。
    """
    # 匹配 <title> 标签内容
    m = re.search(r'<title>(.*?)</title>', html)
    if m:
        t = m.group(1).strip()
        # 去掉 B 站常见的标题后缀
        for sep in ['_哔哩哔哩_bilibili', '-bilibili', '哔哩哔哩']:
            if sep in t:
                t = t.split(sep)[0].strip()
        if t:
            return t
    # 提取失败返回默认值
    return 'video'


def extract_pubdate(playinfo, html):
    """
    从 playinfo 和 HTML 中提取视频发布时间，格式化为 YYYY-MM-DD。

    提取优先级：
    1. playinfo.data.video_info.pubdate
    2. playinfo.data.video_info.ctime
    3. playinfo.data.pubdate / playinfo.data.ctime
    4. HTML 中正则匹配 "pubdate": 数字

    都提取不到则返回 None。

    参数:
        playinfo (dict):
            播放信息字典。
        html (str):
            视频页面 HTML 源代码。

    返回:
        str or None:
            格式化后的日期字符串（如 "2024-01-15"），提取失败返回 None。
    """
    # 先从 playinfo 中找发布时间（可能嵌套在 video_info 里）
    data = playinfo.get('data', playinfo)
    vi = data.get('video_info', {})
    # 尝试多个可能的字段名：pubdate 或 ctime
    pubdate = vi.get('pubdate') or vi.get('ctime') or data.get('pubdate') or data.get('ctime')

    if pubdate:
        try:
            # Unix 时间戳转 datetime，再格式化为 YYYY-MM-DD
            dt = datetime.fromtimestamp(int(pubdate))
            return dt.strftime('%Y-%m-%d')
        except Exception:
            pass

    # playinfo 里找不到，再从 HTML 中正则匹配
    m = re.search(r'"pubdate"\s*:\s*(\d+)', html)
    if m:
        try:
            dt = datetime.fromtimestamp(int(m.group(1)))
            return dt.strftime('%Y-%m-%d')
        except Exception:
            pass

    # 都找不到
    return None


# ==================== 下载模块 ====================

def download_stream(url, path, headers=None, cookies=None):
    """
    下载单个视频/音频流文件，带进度显示。

    使用流式下载（stream=True），一边下载一边写入文件，
    避免大文件占用过多内存。

    参数:
        url (str):
            要下载的文件 URL。
        path (str):
            保存到本地的文件路径。
        headers (dict, 可选):
            请求头。默认为 None。
        cookies (dict, 可选):
            Cookie 字典。默认为 None。
    """
    headers = headers or {}
    cookies = cookies or {}

    logger.info(f"开始下载 -> {os.path.basename(path)}")

    # stream=True 表示流式下载，不会一次性把整个文件读入内存
    with requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=60) as r:
        r.raise_for_status()

        # 获取文件总大小（字节），可能没有这个头
        total = int(r.headers.get('content-length', 0))
        downloaded = 0  # 已下载字节数

        with open(path, 'wb') as f:
            # 分块读取，每块 8KB
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

                    # 如果知道总大小，显示进度百分比
                    if total:
                        pct = downloaded / total * 100
                        # \r 是回车符，回到行首覆盖上一次的输出，实现单行进度条
                        sys.stdout.write(f"\r  进度: {pct:.1f}% ({downloaded//1024}/{total//1024} KB)")
                        sys.stdout.flush()

    # 换行（因为进度条用了 \r，最后需要换行）
    print()
    logger.info(f"下载完成: {path}")


# ==================== FFmpeg 合并模块 ====================

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


# ==================== 单视频下载流程（子进程调用） ====================

def download_single_video(bv, out_dir, headers_base, cookie_string, worker_id=0):
    """
    下载单个视频的完整流程（供工作进程调用）。

    流程：
    1. 请求视频页面
    2. 提取 playinfo JSON
    3. 选择最高画质
    4. 下载视频流
    5. 下载音频流
    6. 合并音视频（如果 FFmpeg 可用）
    7. 返回结果字典

    参数:
        bv (str):
            视频 BV 号。
        out_dir (str):
            输出根目录。
        headers_base (dict):
            基础请求头。
        cookie_string (str):
            Cookie 字符串。
        worker_id (int, 可选):
            工作进程编号，用于日志前缀。默认为 0。

    返回:
        dict:
            结果字典，包含以下字段：
            - bv: BV 号
            - title: 视频标题（成功时）
            - success: 是否成功
            - path: 输出文件路径（成功时）
            - quality: 画质描述（成功时）
            - error: 错误信息（失败时）
            - merged: 是否已合并（成功但合并不成功时为 False）
    """
    # 日志前缀，标记是哪个进程的输出
    prefix = f"[进程{worker_id}] "

    # 构建视频页面 URL
    video_url = f"https://www.bilibili.com/video/{bv}"

    # 构造请求头，设置正确的 Referer（B 站视频流的防盗链检测）
    headers = dict(headers_base)
    headers['Referer'] = video_url

    # 将 Cookie 字符串转换为字典
    cookies = cookie_str_to_dict(cookie_string) if cookie_string else None

    logger.info(f"{prefix}处理视频: {bv}")

    # ========== 第一步：请求视频页面 ==========
    try:
        time.sleep(1)  # 延时 1 秒，模拟真实用户行为，避免触发反爬
        r = requests.get(video_url, headers=headers, cookies=cookies, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        logger.error(f'{prefix}请求页面失败 {bv}: {e}')
        return {'bv': bv, 'success': False, 'error': str(e)}

    # ========== 第二步：提取 playinfo ==========
    playinfo = extract_playinfo_from_html(html)
    if not playinfo:
        logger.error(f'{prefix}未找到 playinfo JSON: {bv}')
        return {'bv': bv, 'success': False, 'error': '未找到 playinfo'}

    # ========== 第三步：选择最高画质 ==========
    best = get_best_quality(playinfo)
    if not best:
        logger.error(f'{prefix}未找到可下载的媒体: {bv}')
        return {'bv': bv, 'success': False, 'error': '无可下载媒体'}

    # ========== 第四步：提取标题和发布时间 ==========
    title = sanitize_filename(extract_title_from_html(html))
    # 提取发布时间（YYYY-MM-DD 格式）
    pubdate = extract_pubdate(playinfo, html)
    # 构建用于文件名的标题：有发布时间就加上，格式为 [YYYY-MM-DD] 标题
    if pubdate:
        file_title = f"[{pubdate}] {title}"
    else:
        file_title = title

    logger.info(f"{prefix}标题: {title}")
    if pubdate:
        logger.info(f"{prefix}发布时间: {pubdate}")
    logger.info(f"{prefix}画质: {best['quality']}")

    # 获取视频和音频的下载地址
    video_url_dl = best.get('video_url')
    audio_url_dl = best.get('audio_url')
    video_path = audio_path = None  # 初始化路径变量

    # ========== 第五步：下载视频 ==========
    if video_url_dl:
        # 从 URL 中提取扩展名，如果没有则默认 .mp4
        video_ext = os.path.splitext(urlparse(video_url_dl).path)[1] or '.mp4'
        video_path = os.path.join(out_dir, 'video', file_title + video_ext)
        try:
            download_stream(video_url_dl, video_path, headers=headers, cookies=cookies)
        except Exception as e:
            logger.error(f'{prefix}视频下载失败 {bv}: {e}')
            return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': False, 'error': f'视频下载失败: {e}'}

    # ========== 第六步：下载音频 ==========
    if audio_url_dl:
        # 从 URL 中提取扩展名，如果没有则默认 .m4a
        audio_ext = os.path.splitext(urlparse(audio_url_dl).path)[1] or '.m4a'
        audio_path = os.path.join(out_dir, 'audio', file_title + audio_ext)
        try:
            download_stream(audio_url_dl, audio_path, headers=headers, cookies=cookies)
        except Exception as e:
            logger.error(f'{prefix}音频下载失败 {bv}: {e}')
            return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': False, 'error': f'音频下载失败: {e}'}

    # ========== 第七步：合并音视频 ==========
    merged_path = os.path.join(out_dir, file_title + '.mp4')
    if video_path and audio_path:
        # 有视频和音频，尝试合并
        if merge_av(video_path, audio_path, merged_path):
            # 合并成功，删除原始分片文件
            try:
                os.remove(video_path)
                os.remove(audio_path)
            except Exception:
                # 删除失败也没关系，文件还在
                pass
            return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': True, 'path': merged_path, 'quality': best['quality']}
        else:
            # 合并失败，保留原始文件
            logger.info(f'{prefix}合并失败，原始文件保留: {bv}')
            return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': True, 'path': video_path, 'quality': best['quality'], 'merged': False}
    elif video_path:
        # 只有视频（DURL 格式），直接返回
        return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': True, 'path': video_path, 'quality': best['quality']}

    # 不应该走到这里
    return {'bv': bv, 'success': False, 'error': '未知错误'}


# ==================== 工作进程入口 ====================

def worker_task(worker_id, bv_batch, out_dir, headers, cookie_string):
    """
    工作进程的主任务函数。

    每个工作进程负责下载一批（最多 2 个）视频，
    逐个下载并收集结果，最后返回给主进程。

    参数:
        worker_id (int):
            工作进程编号（从 0 开始）。
        bv_batch (list):
            本进程负责的 BV 号列表。
        out_dir (str):
            输出目录路径。
        headers (dict):
            请求头配置。
        cookie_string (str):
            Cookie 字符串。

    返回:
        list:
            每个视频的下载结果字典列表。
    """
    logger.info(f"===== 进程 {worker_id} 启动，负责 {len(bv_batch)} 个视频 =====")

    results = []  # 存储所有下载结果

    # 逐个下载分配给本进程的视频
    for i, bv in enumerate(bv_batch):
        logger.info(f"[进程{worker_id}] 第 {i+1}/{len(bv_batch)} 个视频")

        # 调用单视频下载函数
        result = download_single_video(bv, out_dir, headers, cookie_string, worker_id)
        results.append(result)

        # 不是最后一个视频的话，间隔 2 秒再下一个（降低频率，模拟真实用户行为）
        if i < len(bv_batch) - 1:
            time.sleep(2)

    # 统计本进程的成功数量
    success_count = sum(1 for r in results if r['success'])
    logger.info(f"===== 进程 {worker_id} 完成，成功 {success_count}/{len(results)} =====")

    return results


# ==================== 主函数 ====================

def main():
    """
    主函数：程序入口。

    执行流程：
    1. 获取用户输入（关键词、下载数量、Cookie）
    2. 验证 Cookie 有效性
    3. 搜索视频获取 BV 号列表
    4. 按每进程 2 个视频分配任务
    5. 启动多进程并发下载
    6. 收集所有结果，输出详细总结
    """
    # ========== 1. 用户输入 ==========
    keyword = input('请输入搜索关键词: ').strip()
    if not keyword:
        logger.error('未提供关键词')
        return

    count_str = input('请输入下载数量（默认5）: ').strip()
    try:
        count = int(count_str) if count_str else 5
    except ValueError:
        count = 5  # 输入无效则用默认值
    if count <= 0:
        count = 5   # 数量不能小于等于 0

    # 读取 Cookie（优先从文件读取，没有则提示输入）
    cookie_string = ''
    cf = os.path.join(BASE_DIR, 'cookie.txt')
    if os.path.exists(cf):
        with open(cf, 'r', encoding='utf-8') as f:
            cookie_string = f.read().strip()
    if not cookie_string:
        cookie_string = input('请粘贴 cookie（回车跳过）: ').strip()

    # ========== 2. 创建输出目录 ==========
    out_dir = os.path.join(BASE_DIR, 'output')
    os.makedirs(out_dir, exist_ok=True)                   # 主输出目录
    os.makedirs(os.path.join(out_dir, 'video'), exist_ok=True)  # 视频分片目录
    os.makedirs(os.path.join(out_dir, 'audio'), exist_ok=True)  # 音频分片目录

    # ========== 3. 构造请求头 ==========
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    }

    # ========== 4. 任务开始日志 ==========
    logger.info("===== B站搜索爬虫任务开始 =====")
    logger.info(f"搜索关键词: {keyword}")
    logger.info(f"下载数量: {count}")

    # ========== 5. 验证 Cookie ==========
    validate_cookie(cookie_string, headers)

    # ========== 6. 搜索视频 ==========
    cookies_dict = cookie_str_to_dict(cookie_string) if cookie_string else None
    bv_list = search_bilibili(keyword, headers, cookies_dict, count)
    if not bv_list:
        logger.error('未搜索到视频')
        return

    # 截取需要的数量
    actual_count = min(count, len(bv_list))
    bv_list = bv_list[:actual_count]
    logger.info(f"将下载 {actual_count} 个视频")

    # ========== 7. 检测 FFmpeg ==========
    if have_ffmpeg():
        logger.info('FFmpeg 可用，将自动合并音视频')
    else:
        logger.warning('未检测到 FFmpeg，将只下载不合并')

    # ========== 8. 分配进程任务 ==========
    per_worker = 2  # 每个进程最多处理 2 个视频
    # 计算需要的进程数（向上取整）
    num_workers = max(1, (actual_count + per_worker - 1) // per_worker)
    logger.info(f"启动 {num_workers} 个进程，每个进程最多 {per_worker} 个视频")

    # 将 BV 列表按每 2 个一组分配给各个进程
    batches = []
    for i in range(num_workers):
        batch = bv_list[i * per_worker:(i + 1) * per_worker]
        if batch:
            batches.append((i, batch))  # (进程编号, BV列表)

    # 记录开始时间
    start_time = time.time()

    # ========== 9. 执行下载（单进程/多进程自动选择） ==========
    if num_workers == 1:
        # 只有 1 个任务时，直接在当前进程运行（避免多进程开销）
        all_results = worker_task(0, bv_list, out_dir, headers, cookie_string)
    else:
        # 多个任务，使用进程池并发执行
        pool = multiprocessing.Pool(processes=num_workers)
        async_results = []

        # 将每个批次的任务提交给进程池
        for wid, batch in batches:
            async_results.append(pool.apply_async(worker_task, args=(wid, batch, out_dir, headers, cookie_string)))

        pool.close()    # 关闭进程池，不再接受新任务
        pool.join()     # 等待所有子进程执行完毕

        # 收集所有结果
        all_results = []
        for ar in async_results:
            all_results.extend(ar.get())

    # 计算总耗时
    elapsed = time.time() - start_time

    # ========== 10. 输出总结 ==========
    # 分离成功和失败的结果
    success_list = [r for r in all_results if r.get('success')]
    fail_list = [r for r in all_results if not r.get('success')]

    print()  # 空一行分隔
    logger.info("===== 任务全部执行完毕 =====")
    logger.info(f"总耗时: {elapsed:.1f} 秒")
    logger.info(f"成功: {len(success_list)} 个")
    logger.info(f"失败: {len(fail_list)} 个")

    # 成功列表
    if success_list:
        logger.info("成功列表:")
        for r in success_list:
            logger.info(f"  ✓ {r.get('bv', '?')} - {r.get('title', '?')} [{r.get('quality', '?')}]")

    # 失败列表
    if fail_list:
        logger.info("失败列表:")
        for r in fail_list:
            logger.info(f"  ✗ {r.get('bv', '?')} - {r.get('error', '未知错误')}")

    logger.info(f"输出目录: {out_dir}")


# ==================== 程序入口 ====================
if __name__ == '__main__':
    """
    程序入口点。
    当脚本被直接运行时（而不是被导入时），执行 main() 函数。
    运行结束后等待用户按回车再退出，方便查看输出结果。
    """
    main()
    input('按回车退出...')
