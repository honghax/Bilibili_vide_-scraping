import json
import os
import re
import sys
import time
import shutil
import requests
import logging
import subprocess
import multiprocessing
from urllib.parse import urlparse, quote
from datetime import datetime

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


def search_bilibili(keyword, headers, cookies=None, max_results=50):
    logger.info(f"搜索关键词: {keyword}")
    search_url = f"https://search.bilibili.com/all?keyword={quote(keyword)}"
    search_headers = dict(headers)
    search_headers['Referer'] = 'https://www.bilibili.com/'
    try:
        r = requests.get(search_url, headers=search_headers, cookies=cookies, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        logger.error(f'搜索请求失败: {e}')
        return []

    bv_pattern = r'//www\.bilibili\.com/video/(BV[0-9a-zA-Z]{10})'
    matches = re.findall(bv_pattern, html)
    seen = set()
    bv_list = []
    for bv in matches:
        if bv not in seen:
            seen.add(bv)
            bv_list.append(bv)

    result = bv_list[:max_results]
    logger.info(f"搜索到 {len(bv_list)} 个视频，取前 {len(result)} 个")
    return result


def extract_playinfo_from_html(html):
    prefixes = [
        'window.__playinfo__ =',
        'window.__playinfo__=',
        '__playinfo__ =',
    ]
    for p in prefixes:
        idx = html.find(p)
        if idx == -1:
            continue
        start = idx + len(p)
        while start < len(html) and html[start] in ' \t\n\r=':
            start += 1
        if start >= len(html) or html[start] != '{':
            continue
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


def get_best_quality(playinfo):
    data = playinfo.get('data', playinfo)
    dash = data.get('dash', {})
    vlist = dash.get('video', [])
    alist = dash.get('audio', [])
    if not vlist:
        durl = data.get('durl')
        if durl:
            best = max(durl, key=lambda x: x.get('size', 0))
            return {
                'type': 'durl',
                'video_url': best.get('url') or best.get('backup_url'),
                'audio_url': None,
                'quality': 'durl直链',
            }
        return None

    quality_groups = {}
    for v in vlist:
        qid = v.get('id', v.get('quality', 0))
        if qid not in quality_groups or v.get('bandwidth', 0) > quality_groups[qid].get('bandwidth', 0):
            quality_groups[qid] = v
    best_qid = max(quality_groups.keys())
    best_v = quality_groups[best_qid]
    best_a = max(alist, key=lambda x: x.get('bandwidth', 0)) if alist else None
    return {
        'type': 'dash',
        'video_url': best_v.get('baseUrl') or best_v.get('base_url'),
        'audio_url': best_a.get('baseUrl') or best_a.get('base_url') if best_a else None,
        'quality': f"{best_qid} ({best_v.get('width', '?')}x{best_v.get('height', '?')})",
    }


def extract_title_from_html(html):
    m = re.search(r'<title>(.*?)</title>', html)
    if m:
        t = m.group(1).strip()
        for sep in ['_哔哩哔哩_bilibili', '-bilibili', '哔哩哔哩']:
            if sep in t:
                t = t.split(sep)[0].strip()
        if t:
            return t
    return 'video'


def extract_pubdate(playinfo, html):
    data = playinfo.get('data', playinfo)
    vi = data.get('video_info', {})
    pubdate = vi.get('pubdate') or vi.get('ctime') or data.get('pubdate') or data.get('ctime')
    if pubdate:
        try:
            dt = datetime.fromtimestamp(int(pubdate))
            return dt.strftime('%Y-%m-%d')
        except Exception:
            pass
    m = re.search(r'"pubdate"\s*:\s*(\d+)', html)
    if m:
        try:
            dt = datetime.fromtimestamp(int(m.group(1)))
            return dt.strftime('%Y-%m-%d')
        except Exception:
            pass
    return None


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


def download_single_video(bv, out_dir, headers_base, cookie_string, worker_id=0):
    prefix = f"[进程{worker_id}] "
    video_url = f"https://www.bilibili.com/video/{bv}"
    headers = dict(headers_base)
    headers['Referer'] = video_url
    cookies = cookie_str_to_dict(cookie_string) if cookie_string else None

    logger.info(f"{prefix}处理视频: {bv}")

    try:
        time.sleep(1)
        r = requests.get(video_url, headers=headers, cookies=cookies, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        logger.error(f'{prefix}请求页面失败 {bv}: {e}')
        return {'bv': bv, 'success': False, 'error': str(e)}

    playinfo = extract_playinfo_from_html(html)
    if not playinfo:
        logger.error(f'{prefix}未找到 playinfo JSON: {bv}')
        return {'bv': bv, 'success': False, 'error': '未找到 playinfo'}

    best = get_best_quality(playinfo)
    if not best:
        logger.error(f'{prefix}未找到可下载的媒体: {bv}')
        return {'bv': bv, 'success': False, 'error': '无可下载媒体'}

    title = sanitize_filename(extract_title_from_html(html))
    pubdate = extract_pubdate(playinfo, html)
    if pubdate:
        file_title = f"[{pubdate}] {title}"
    else:
        file_title = title
    logger.info(f"{prefix}标题: {title}")
    if pubdate:
        logger.info(f"{prefix}发布时间: {pubdate}")
    logger.info(f"{prefix}画质: {best['quality']}")

    video_url_dl = best.get('video_url')
    audio_url_dl = best.get('audio_url')
    video_path = audio_path = None

    if video_url_dl:
        video_ext = os.path.splitext(urlparse(video_url_dl).path)[1] or '.mp4'
        video_path = os.path.join(out_dir, 'video', file_title + video_ext)
        try:
            download_stream(video_url_dl, video_path, headers=headers, cookies=cookies)
        except Exception as e:
            logger.error(f'{prefix}视频下载失败 {bv}: {e}')
            return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': False, 'error': f'视频下载失败: {e}'}

    if audio_url_dl:
        audio_ext = os.path.splitext(urlparse(audio_url_dl).path)[1] or '.m4a'
        audio_path = os.path.join(out_dir, 'audio', file_title + audio_ext)
        try:
            download_stream(audio_url_dl, audio_path, headers=headers, cookies=cookies)
        except Exception as e:
            logger.error(f'{prefix}音频下载失败 {bv}: {e}')
            return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': False, 'error': f'音频下载失败: {e}'}

    merged_path = os.path.join(out_dir, file_title + '.mp4')
    if video_path and audio_path:
        if merge_av(video_path, audio_path, merged_path):
            try:
                os.remove(video_path)
                os.remove(audio_path)
            except Exception:
                pass
            return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': True, 'path': merged_path, 'quality': best['quality']}
        else:
            logger.info(f'{prefix}合并失败，原始文件保留: {bv}')
            return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': True, 'path': video_path, 'quality': best['quality'], 'merged': False}
    elif video_path:
        return {'bv': bv, 'title': title, 'pubdate': pubdate, 'success': True, 'path': video_path, 'quality': best['quality']}

    return {'bv': bv, 'success': False, 'error': '未知错误'}


def worker_task(worker_id, bv_batch, out_dir, headers, cookie_string):
    logger.info(f"===== 进程 {worker_id} 启动，负责 {len(bv_batch)} 个视频 =====")
    results = []
    for i, bv in enumerate(bv_batch):
        logger.info(f"[进程{worker_id}] 第 {i+1}/{len(bv_batch)} 个视频")
        result = download_single_video(bv, out_dir, headers, cookie_string, worker_id)
        results.append(result)
        if i < len(bv_batch) - 1:
            time.sleep(2)
    logger.info(f"===== 进程 {worker_id} 完成，成功 {sum(1 for r in results if r['success'])}/{len(results)} =====")
    return results


def main():
    keyword = input('请输入搜索关键词: ').strip()
    if not keyword:
        logger.error('未提供关键词')
        return

    count_str = input('请输入下载数量（默认5）: ').strip()
    try:
        count = int(count_str) if count_str else 5
    except ValueError:
        count = 5
    if count <= 0:
        count = 5

    cookie_string = ''
    cf = os.path.join(BASE_DIR, 'cookie.txt')
    if os.path.exists(cf):
        with open(cf, 'r', encoding='utf-8') as f:
            cookie_string = f.read().strip()
    if not cookie_string:
        cookie_string = input('请粘贴 cookie（回车跳过）: ').strip()

    out_dir = os.path.join(BASE_DIR, 'output')
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, 'video'), exist_ok=True)
    os.makedirs(os.path.join(out_dir, 'audio'), exist_ok=True)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    }

    logger.info("===== B站搜索爬虫任务开始 =====")
    logger.info(f"搜索关键词: {keyword}")
    logger.info(f"下载数量: {count}")

    validate_cookie(cookie_string, headers)

    bv_list = search_bilibili(keyword, headers, cookie_str_to_dict(cookie_string) if cookie_string else None, count)
    if not bv_list:
        logger.error('未搜索到视频')
        return

    actual_count = min(count, len(bv_list))
    bv_list = bv_list[:actual_count]
    logger.info(f"将下载 {actual_count} 个视频")

    if have_ffmpeg():
        logger.info('FFmpeg 可用，将自动合并音视频')
    else:
        logger.warning('未检测到 FFmpeg，将只下载不合并')

    per_worker = 2
    num_workers = max(1, (actual_count + per_worker - 1) // per_worker)
    logger.info(f"启动 {num_workers} 个进程，每个进程最多 {per_worker} 个视频")

    batches = []
    for i in range(num_workers):
        batch = bv_list[i * per_worker:(i + 1) * per_worker]
        if batch:
            batches.append((i, batch))

    start_time = time.time()

    if num_workers == 1:
        all_results = worker_task(0, bv_list, out_dir, headers, cookie_string)
    else:
        pool = multiprocessing.Pool(processes=num_workers)
        async_results = []
        for wid, batch in batches:
            async_results.append(pool.apply_async(worker_task, args=(wid, batch, out_dir, headers, cookie_string)))
        pool.close()
        pool.join()
        all_results = []
        for ar in async_results:
            all_results.extend(ar.get())

    elapsed = time.time() - start_time

    success_list = [r for r in all_results if r.get('success')]
    fail_list = [r for r in all_results if not r.get('success')]

    print()
    logger.info("===== 任务全部执行完毕 =====")
    logger.info(f"总耗时: {elapsed:.1f} 秒")
    logger.info(f"成功: {len(success_list)} 个")
    logger.info(f"失败: {len(fail_list)} 个")

    if success_list:
        logger.info("成功列表:")
        for r in success_list:
            logger.info(f"  ✓ {r.get('bv', '?')} - {r.get('title', '?')} [{r.get('quality', '?')}]")

    if fail_list:
        logger.info("失败列表:")
        for r in fail_list:
            logger.info(f"  ✗ {r.get('bv', '?')} - {r.get('error', '未知错误')}")

    logger.info(f"输出目录: {out_dir}")


if __name__ == '__main__':
    main()
    input('按回车退出...')
