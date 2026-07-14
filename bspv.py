import requests
import time
import re
import json
import logging
import os


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "bilibili_spider.txt")

logger = logging.getLogger()
logger.setLevel(logging.INFO)
log_format = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")


console_handler = logging.StreamHandler()
console_handler.setFormatter(log_format)
logger.addHandler(console_handler)


file_handler = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
file_handler.setFormatter(log_format)
logger.addHandler(file_handler)


bv = input('请输入BV号：')
base_url = "https://www.bilibili.com"

url = f"{base_url}/video/{bv}"

headers = {
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Referer": url,
}
cookie_string = input("请输入B站cookie或从cookie.txt读取：").strip()
if not cookie_string:
    cookie_file_path = os.path.join(BASE_DIR, "cookie.txt")
    try:
        with open(cookie_file_path, "r", encoding="utf-8") as f:
            cookie_string = f.read().strip()
    except FileNotFoundError:
        logger.warning(f"cookie.txt 未找到：{cookie_file_path}")
        cookie_string = ""
    if not cookie_string:
        logger.warning("未提供有效的cookie，请在cookie.txt中设置或在运行时输入。")
        raise ValueError("未提供有效的cookie，请在cookie.txt中设置或在运行时输入。")
    

cookies = {pair.split("=", 1)[0]: pair.split("=", 1)[1] for pair in cookie_string.split("; ") if "=" in pair}


def sanitize_filename(name, fallback="file"):
    """去掉文件名中的非法字符，避免保存时报错。"""
    safe_name = os.path.basename(str(name)).strip()
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '', safe_name)
    safe_name = re.sub(r'[.\s]+$', '', safe_name)
    return safe_name or fallback


def validate_cookie(cookie_value):
    if not cookie_value.strip():
        logger.warning("未设置 cookie，继续以匿名方式请求。")
        return False
    logger.info("开始验证 cookie 是否有效...")
    check_headers = dict(headers)
    check_headers["Cookie"] = cookie_value
    try:
        resp = requests.get(
            url="https://api.bilibili.com/x/web-interface/nav",
            headers=check_headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        is_login = data.get("data", {}).get("isLogin")
        if is_login:
            logger.info("Cookie 验证通过：有效登录状态。")
            return True
        logger.warning("Cookie 验证失败：该 cookie 无效或未登录。")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Cookie 验证请求异常：{e}")
        return False
    except ValueError:
        logger.error("Cookie 验证响应不是有效 JSON。")
        return False


def main():
    try:
        logger.info("===== B站爬虫任务开始 =====")
        logger.info(f"目标BV号：{bv}，访问链接：{url}")

        logger.info("正在请求视频网页...")
        validate_cookie(cookie_string)
        response = requests.get(url=url, headers=headers, cookies=cookies, timeout=15)
        response.raise_for_status() 
        html = response.text
        logger.info("网页请求成功，开始解析页面数据")

     
        title = re.findall('title="(.*?)"', html)[0]
        logger.info(f"提取到视频标题：{title}")

      
        playinfo_str = re.findall('window.__playinfo__=(.*?)</script>', html)[0]
        json_data = json.loads(playinfo_str)
        logger.info("成功解析播放信息JSON")

        video_list = json_data['data']['dash']['video']
        logger.info("视频清晰度候选项：" + ", ".join(
            f"{v['id']}({v.get('width', '?')}x{v.get('height', '?')},bw={v.get('bandwidth', '?')})"
            for v in video_list
        ))
        target_id = int(input("请输入目标清晰度ID："))  
        target_video = None
        for v in video_list:
            if v["id"] == target_id:
                target_video = v
                break

        if not target_video:
            target_video = max(video_list, key=lambda x: x["bandwidth"])
            logger.info(f"未找到目标清晰度{target_id}，改为最高码率画质")
        video_url = target_video["baseUrl"]
        logger.info(f"选中画质id:{target_video['id']} {target_video['width']}×{target_video['height']}")

        audio_list = json_data['data']['dash']['audio']
        best_audio = max(audio_list, key=lambda x: x["bandwidth"])
        audio_url = best_audio["baseUrl"]

     
        video_dir = os.path.join(BASE_DIR, "video")
        audio_dir = os.path.join(BASE_DIR, "audio")

        if not os.path.exists(video_dir):
            os.mkdir(video_dir)
            logger.info(f"video文件夹不存在，已自动创建：{video_dir}")

        if not os.path.exists(audio_dir):
            os.mkdir(audio_dir)
            logger.info(f"audio文件夹不存在，已自动创建：{audio_dir}")

        safe_title = sanitize_filename(title)

        Video_save = input("是否保存视频文件？(y/n): ").strip().lower()
        logger.info(f"用户选择保存视频文件：{Video_save}")
        if Video_save == 'y':
         logger.info("开始下载视频文件...")
         video_content = requests.get(url=video_url, headers=headers, cookies=cookies, timeout=30).content
         video_save_path = os.path.join(video_dir, safe_title + ".mp4")
         with open(video_save_path, mode='wb') as v:
            v.write(video_content)
         logger.info(f"视频保存完成：{video_save_path}")
        
        Audio_save = input("是否保存音频文件？(y/n): ").strip().lower()
        logger.info(f"用户选择保存音频文件：{Audio_save}")
        if Audio_save == 'y':
         logger.info("开始下载音频文件...")
         audio_content = requests.get(url=audio_url, headers=headers, cookies=cookies, timeout=30).content
         audio_save_path = os.path.join(audio_dir, safe_title + ".mp3")
         with open(audio_save_path, mode='wb') as a:
            a.write(audio_content)
         logger.info(f"音频保存完成：{audio_save_path}")

        logger.info("===== B站爬虫任务全部执行完毕 =====\n")

    except requests.exceptions.RequestException as e:
        logger.error(f"网络请求异常：{e}", exc_info=False)
    except IndexError:
        logger.error("正则匹配失败，页面结构改变，无法提取标题/播放链接")
    except KeyError as e:
        logger.error(f"JSON数据缺少关键字段：{e}，播放信息解析失败")
    except Exception as e:
        logger.error(f"程序未知错误：{e}", exc_info=False)

if __name__ == "__main__":
    main()
    input("按回车键退出程序...")