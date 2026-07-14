import json
import os
import re
import sys
import shutil
import requests
import logging
import subprocess
from urllib.parse import urlparse

try:
    import ffmpeg
    _HAS_FFMPEG_PY = True
except ImportError:
    ffmpeg = None
    _HAS_FFMPEG_PY = False


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FFMPEG_PATH = os.environ.get('FFMPEG_PATH', '') or shutil.which('ffmpeg') or ''

LOG_PATH = os.path.join(BASE_DIR, "bilibili_spider.txt")

logger = logging.getLogger("bili_spider")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

ch = logging.StreamHandler()
ch.setFormatter(fmt)
logger.addHandler(ch)

fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
fh.setFormatter(fmt)
logger.addHandler(fh)


def sanitize_filename(name, fallback="file"):
    safe = os.path.basename(str(name)).strip()
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '', safe)
    safe = re.sub(r'[.\s]+$', '', safe)
    return safe or fallback


def cookie_str_to_dict(cookie_string):
    return {pair.split('=', 1)[0]: pair.split('=', 1)[1] for pair in cookie_string.split('; ') if '=' in pair}


def validate_cookie(cookie_string, headers):
    if not cookie_string:
        logger.warning("没有提供 cookie，使用匿名请求。")
        return False
    check_headers = dict(headers)
    check_headers['Cookie'] = cookie_string
    try:
        r = requests.get('https://api.bilibili.com/x/web-interface/nav', headers=check_headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get('data', {}).get('isLogin'):
            logger.info('Cookie 有效，已登录')
            return True
        logger.warning('Cookie 未登录或无效')
        return False
    except Exception as e:
        logger.warning(f'验证 cookie 时异常: {e}')
        return False


def extract_json_after_prefix(html, prefix):
    idx = html.find(prefix)
    if idx == -1:
        return None
    start = idx + len(prefix)
    while start < len(html) and html[start] in ' \t\n\r':
        start += 1
    if start >= len(html) or html[start] != '{':
        return None
    depth = 0
    i = start
    in_string = False
    escape = False
    while i < len(html):
        ch = html[i]
        if escape:
            escape = False
        elif ch == '\\':
            escape = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:i+1])
                    except json.JSONDecodeError:
                        return None
        i += 1
    return None


def extract_playinfo_from_html(html):
    prefixes = [
        'const playurlSSRData =',
        'playurlSSRData =',
        'window.__PLAYURL_HYDRATE_DATA__ =',
        'window.__playinfo__ =',
        '__playinfo__ =',
    ]
    for p in prefixes:
        data = extract_json_after_prefix(html, p)
        if data:
            return data
    return None


def get_playinfo_normalized(parsed):
    if not parsed:
        return None
    if isinstance(parsed.get('data'), dict):
        data = parsed['data']
        if isinstance(data.get('result'), dict):
            res = data['result']
            vi = res.get('video_info', {})
            has_dash = bool(res.get('dash')) or bool(vi.get('dash'))
            has_durl = bool(res.get('durl')) or bool(vi.get('durl'))
            if has_dash or has_durl:
                if not res.get('dash') and vi.get('dash'):
                    res['dash'] = vi['dash']
                if not res.get('durl') and vi.get('durl'):
                    res['durl'] = vi['durl']
                return res
        if 'dash' in data or 'durl' in data:
            return data
    if 'dash' in parsed or 'durl' in parsed:
        return parsed
    if isinstance(parsed.get('result'), dict):
        return parsed['result']
    return None


def list_qualities(playinfo):
    dash = playinfo.get('dash', {})
    vlist = dash.get('video', [])
    alist = dash.get('audio', [])
    qualities = []

    if vlist:
        best_a = max(alist, key=lambda x: x.get('bandwidth', 0)) if alist else None
        quality_groups = {}
        for v in vlist:
            qid = v.get('id', v.get('quality', 0))
            if qid not in quality_groups or v.get('bandwidth', 0) > quality_groups[qid].get('bandwidth', 0):
                quality_groups[qid] = v
        sorted_qids = sorted(quality_groups.keys(), reverse=True)
        for qid in sorted_qids:
            v = quality_groups[qid]
            qualities.append({
                'type': 'dash',
                'video': v,
                'audio': best_a,
                'desc': f"{qid} ({v.get('width', '?')}x{v.get('height', '?')}) - DASH",
            })

    if playinfo.get('durl'):
        sorted_d = sorted(playinfo['durl'], key=lambda x: x.get('size', 0), reverse=True)
        for d in sorted_d:
            qualities.append({
                'type': 'durl',
                'durl': d,
                'desc': f"{playinfo.get('video_info', {}).get('quality', '?')} (durl直链)",
            })

    return qualities


def choose_quality(playinfo):
    qlist = list_qualities(playinfo)
    if not qlist:
        return None

    print()
    print('可选画质：')
    for i, q in enumerate(qlist):
        print(f"  {i + 1}. {q['desc']}")

    choice = input(f'请选择画质（默认 1，最高画质）: ').strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(qlist):
            selected = qlist[idx]
        else:
            selected = qlist[0]
    except ValueError:
        selected = qlist[0]

    if selected['type'] == 'dash':
        v = selected['video']
        a = selected['audio']
        return {
            'video_url': v.get('baseUrl') or v.get('base_url'),
            'audio_url': a.get('baseUrl') or a.get('base_url') if a else None,
            'quality': selected['desc'],
        }
    else:
        d = selected['durl']
        return {
            'video_url': d.get('url') or d.get('backup_url'),
            'audio_url': None,
            'quality': selected['desc'],
        }


def extract_title(html, playinfo):
    sup = playinfo.get('supplement', {}) if playinfo else {}
    ep = sup.get('ogv_episode_info', {})
    og_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
    season = og_title.group(1).strip() if og_title else ''
    if season and ep.get('index_title') and ep.get('long_title'):
        return f"{season} 第{ep['index_title']}集：{ep['long_title']}"
    if season and ep.get('index_title'):
        return f"{season} 第{ep['index_title']}集"
    if season:
        return season
    for pat in [r'<title>(.*?)</title>', r'<meta\s+itemProp="name"\s+content="([^"]+)"']:
        m = re.search(pat, html)
        if m:
            t = m.group(1).strip()
            for sep in ['-番剧-', '_哔哩哔哩_bilibili', '-bilibili']:
                if sep in t:
                    t = t.split(sep)[0].strip()
            if t:
                return t
    return urlparse(html).path.replace('/', '_') if html else 'video'


def download_stream(url, path, headers=None, cookies=None):
    headers = headers or {}
    cookies = cookies or {}
    logger.info(f"开始下载 -> {os.path.basename(path)}")
    with requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get('content-length', 0))
        downloaded = 0
        with open(path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        sys.stdout.write(f"\r  进度: {pct:.1f}% ({downloaded//1024}/{total//1024} KB)")
                        sys.stdout.flush()
    print()
    logger.info(f"下载完成: {path}")


def have_ffmpeg():
    if FFMPEG_PATH and os.path.isfile(FFMPEG_PATH):
        return True
    if _HAS_FFMPEG_PY:
        try:
            ffmpeg.probe('__nonexistent__')
        except ffmpeg.Error:
            return True
        except Exception:
            pass
    return bool(shutil.which('ffmpeg'))


def merge_av(video_path, audio_path, out_path):
    if not have_ffmpeg():
        return False
    logger.info('ffmpeg 合并音视频...')
    ffmpeg_exe = FFMPEG_PATH or 'ffmpeg'
    use_py = _HAS_FFMPEG_PY and not FFMPEG_PATH
    try:
        if use_py:
            input_v = ffmpeg.input(video_path)
            if audio_path:
                input_a = ffmpeg.input(audio_path)
                ffmpeg.output(input_v, input_a, out_path, c='copy').run(overwrite_output=True, quiet=True)
            else:
                ffmpeg.output(input_v, out_path, c='copy').run(overwrite_output=True, quiet=True)
        else:
            cmd = [ffmpeg_exe, '-y', '-i', video_path]
            if audio_path:
                cmd += ['-i', audio_path, '-c', 'copy']
            else:
                cmd += ['-c', 'copy']
            cmd.append(out_path)
            subprocess.run(cmd, check=True, capture_output=True)
        logger.info(f'合并完成: {out_path}')
        return True
    except Exception as e:
        logger.error(f'ffmpeg 合并失败: {e}')
        return False


def main():
    url = input('请输入B站番剧URL: ').strip()
    if not url:
        logger.error('未提供 URL')
        return

    cookie_string = ''
    cf = os.path.join(BASE_DIR, 'cookie.txt')
    if os.path.exists(cf):
        with open(cf, 'r', encoding='utf-8') as f:
            cookie_string = f.read().strip()
    if not cookie_string:
        cookie_string = input('请粘贴 cookie（回车跳过）: ').strip()

    out_dir = os.path.join(BASE_DIR, 'downloads')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        'Referer': url,
    }

    validate_cookie(cookie_string, headers)
    cookies = cookie_str_to_dict(cookie_string) if cookie_string else None

    try:
        r = requests.get(url, headers=headers, cookies=cookies, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        logger.error(f'请求页面失败: {e}')
        return

    parsed = extract_playinfo_from_html(html)
    if not parsed:
        logger.error('未找到 playinfo JSON')
        return

    playinfo = get_playinfo_normalized(parsed)
    if not playinfo:
        logger.error('解析不到 dash/durl 信息')
        return

    pick = choose_quality(playinfo)
    if not pick:
        logger.error('未找到可下载的媒体链接')
        return

    os.makedirs(out_dir, exist_ok=True)
    video_dir = os.path.join(out_dir, 'video')
    audio_dir = os.path.join(out_dir, 'audio')
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(audio_dir, exist_ok=True)

    title = sanitize_filename(extract_title(html, playinfo))
    logger.info(f'标题: {title}')
    logger.info(f'画质: {pick["quality"]}')

    video_url = pick.get('video_url')
    audio_url = pick.get('audio_url')
    video_path = audio_path = None

    if video_url:
        video_ext = os.path.splitext(urlparse(video_url).path)[1] or '.mp4'
        video_path = os.path.join(video_dir, title + video_ext)
        download_stream(video_url, video_path, headers=headers, cookies=cookies)

    if audio_url:
        audio_ext = os.path.splitext(urlparse(audio_url).path)[1] or '.m4a'
        audio_path = os.path.join(audio_dir, title + audio_ext)
        download_stream(audio_url, audio_path, headers=headers, cookies=cookies)

    if video_path and audio_path:
        merged = os.path.join(out_dir, title + '.mp4')
        print()
        do_merge = input('是否使用 FFmpeg 合并音视频为一个 MP4 文件？(Y/n): ').strip().lower()
        if do_merge in ('', 'y', 'yes'):
            if merge_av(video_path, audio_path, merged):
                try:
                    os.remove(video_path)
                    os.remove(audio_path)
                except Exception:
                    pass
            else:
                logger.info('合并失败，原始文件保留在 video/ 和 audio/ 子目录')
        else:
            logger.info('跳过合并，视频在 video/ 目录，音频在 audio/ 目录')
    elif video_path:
        logger.info('完成: ' + video_path)


if __name__ == '__main__':
    main()
    input('按回车退出...')
