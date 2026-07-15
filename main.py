import os
import sys
import json
import time
import subprocess
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SCRIPTS = {
    "bspv": os.path.join(BASE_DIR, "bspv.py"),
    "bsps": os.path.join(BASE_DIR, "bsps.py"),
    "bsp":  os.path.join(BASE_DIR, "bsp.py"),
}

OUTPUT_DIR = os.path.join(BASE_DIR, "output")
LOG_DIR = os.path.join(BASE_DIR, "log")
COOKIE_FILE = os.path.join(BASE_DIR, "cookie.txt")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


def load_cookie():
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def save_cookie(cookie_str):
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(cookie_str.strip())


def run_script(script_path, inputs, task_name="任务"):
    """运行一个脚本，自动输入参数。返回 (成功, 输出日志路径)"""
    print(f"\n{'='*60}")
    print(f"  开始执行: {task_name}")
    print(f"  脚本: {os.path.basename(script_path)}")
    print(f"{'='*60}\n")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"{timestamp}_{task_name}_runner.log")

    input_text = "\n".join(str(x) for x in inputs) + "\n"
    input_bytes = input_text.encode("utf-8")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        proc = subprocess.Popen(
            [sys.executable, script_path],
            stdin=subprocess.PIPE,
            stdout=sys.stdout,
            stderr=sys.stderr,
            cwd=BASE_DIR,
            env=env,
        )

        try:
            proc.communicate(input=input_bytes, timeout=None)
        except Exception as e:
            print(f"[错误] 脚本执行异常: {e}")
            return False, log_file

        success = proc.returncode == 0

        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"任务名称: {task_name}\n")
            f.write(f"脚本: {os.path.basename(script_path)}\n")
            f.write(f"开始时间: {timestamp}\n")
            f.write(f"返回码: {proc.returncode}\n")
            f.write(f"状态: {'成功' if success else '失败'}\n")
            f.write(f"输入参数:\n")
            for i, inp in enumerate(inputs):
                if "cookie" in str(inp).lower() or len(str(inp)) > 50:
                    f.write(f"  [{i+1}] (已隐藏)\n")
                else:
                    f.write(f"  [{i+1}] {inp}\n")

        print(f"\n  [完成] {task_name} - {'成功' if success else '失败'}")
        return success, log_file

    except FileNotFoundError:
        print(f"[错误] 找不到脚本: {script_path}")
        return False, log_file
    except Exception as e:
        print(f"[错误] 启动脚本失败: {e}")
        return False, log_file


def download_single_video(bv, quality="", cookie=""):
    """调用 bspv.py 下载单个视频"""
    inputs = [bv]
    if cookie:
        inputs.append(cookie)
    else:
        inputs.append("")
    if quality:
        inputs.append(str(quality))
    return run_script(SCRIPTS["bspv"], inputs, f"单视频_{bv}")


def search_and_download(keyword, count=5, cookie=""):
    """调用 bsps.py 搜索并批量下载"""
    inputs = [keyword, str(count)]
    if cookie:
        inputs.append(cookie)
    return run_script(SCRIPTS["bsps"], inputs, f"批量下载_{keyword}")


def download_pgc(url, quality="", cookie=""):
    """调用 bsp.py 下载番剧"""
    inputs = [url]
    if cookie:
        inputs.append(cookie)
    else:
        inputs.append("")
    if quality:
        inputs.append(str(quality))
    return run_script(SCRIPTS["bsp"], inputs, f"番剧下载")


def interactive_menu():
    cookie = load_cookie()

    print("\n" + "="*60)
    print("  B站下载工具箱 - 总控程序")
    print("="*60)

    if cookie:
        print(f"  Cookie: 已加载 ({len(cookie)} 字符)")
    else:
        print("  Cookie: 未设置")

    print(f"  输出目录: {OUTPUT_DIR}")
    print(f"  日志目录: {LOG_DIR}")
    print("="*60)

    while True:
        print("\n请选择功能:")
        print("  1. 单视频下载 (BV号)")
        print("  2. 搜索批量下载")
        print("  3. 番剧下载 (PGC)")
        print("  4. 批量混合任务 (依次执行多种下载)")
        print("  5. 设置 Cookie")
        print("  0. 退出")

        choice = input("\n请输入选项 (0-5): ").strip()

        if choice == "0":
            print("再见！")
            break

        elif choice == "1":
            bv = input("请输入BV号: ").strip()
            if not bv:
                print("BV号不能为空")
                continue
            quality = input("请输入画质ID (回车默认最高): ").strip()
            download_single_video(bv, quality, cookie)

        elif choice == "2":
            keyword = input("请输入搜索关键词: ").strip()
            if not keyword:
                print("关键词不能为空")
                continue
            count_str = input("请输入下载数量 (默认5): ").strip()
            try:
                count = int(count_str) if count_str else 5
            except ValueError:
                count = 5
            search_and_download(keyword, count, cookie)

        elif choice == "3":
            url = input("请输入番剧URL: ").strip()
            if not url:
                print("URL不能为空")
                continue
            download_pgc(url, "", cookie)

        elif choice == "4":
            batch_mode(cookie)

        elif choice == "5":
            new_cookie = input("请粘贴Cookie (留空跳过): ").strip()
            if new_cookie:
                save_cookie(new_cookie)
                cookie = new_cookie
                print(f"Cookie 已保存 ({len(cookie)} 字符)")
            else:
                print("未修改")

        else:
            print("无效选项，请重新输入")


def batch_mode(cookie):
    """批量混合任务模式"""
    print("\n" + "="*60)
    print("  批量混合任务模式")
    print("  依次输入多个任务，按顺序执行")
    print("="*60)
    print("\n任务格式:")
    print("  单视频:  bv <BV号> [画质ID]")
    print("  搜索下载: search <关键词> [数量]")
    print("  番剧下载: pgc <URL>")
    print("  输入 'done' 开始执行，'clear' 清空，'list' 查看列表")
    print()

    tasks = []

    while True:
        cmd = input(f"[{len(tasks)}个任务] 请输入: ").strip()

        if not cmd:
            continue
        if cmd.lower() == "done":
            break
        if cmd.lower() == "clear":
            tasks = []
            print("已清空任务列表")
            continue
        if cmd.lower() == "list":
            print(f"\n当前任务列表 ({len(tasks)} 个):")
            for i, t in enumerate(tasks, 1):
                print(f"  {i}. {t['desc']}")
            print()
            continue

        parts = cmd.split()
        task_type = parts[0].lower()

        if task_type == "bv" and len(parts) >= 2:
            bv = parts[1]
            quality = parts[2] if len(parts) > 2 else ""
            tasks.append({
                "type": "bv",
                "bv": bv,
                "quality": quality,
                "desc": f"单视频下载: {bv}" + (f" 画质:{quality}" if quality else " 最高画质"),
            })
            print(f"  + 添加任务: 单视频 {bv}")

        elif task_type == "search" and len(parts) >= 2:
            keyword = " ".join(parts[1:-1]) if parts[-1].isdigit() else " ".join(parts[1:])
            count = int(parts[-1]) if parts[-1].isdigit() else 5
            tasks.append({
                "type": "search",
                "keyword": keyword,
                "count": count,
                "desc": f"搜索下载: {keyword} ({count}个)",
            })
            print(f"  + 添加任务: 搜索 {keyword} ({count}个)")

        elif task_type == "pgc" and len(parts) >= 2:
            url = parts[1]
            tasks.append({
                "type": "pgc",
                "url": url,
                "desc": f"番剧下载: {url}",
            })
            print(f"  + 添加任务: 番剧 {url}")

        else:
            print("  无法识别的任务格式")

    if not tasks:
        print("没有任务，返回主菜单")
        return

    print(f"\n即将执行 {len(tasks)} 个任务:")
    for i, t in enumerate(tasks, 1):
        print(f"  {i}. {t['desc']}")

    confirm = input("\n确认开始执行? (Y/n): ").strip().lower()
    if confirm not in ("", "y", "yes"):
        print("已取消")
        return

    results = []
    start_time = time.time()

    for i, task in enumerate(tasks, 1):
        print(f"\n\n{'#'*60}")
        print(f"#  任务 {i}/{len(tasks)}: {task['desc']}")
        print(f"{'#'*60}")

        if task["type"] == "bv":
            ok, log = download_single_video(task["bv"], task.get("quality", ""), cookie)
        elif task["type"] == "search":
            ok, log = search_and_download(task["keyword"], task["count"], cookie)
        elif task["type"] == "pgc":
            ok, log = download_pgc(task["url"], "", cookie)
        else:
            ok, log = False, ""

        results.append({
            "task": task,
            "success": ok,
            "log": log,
        })

    elapsed = time.time() - start_time

    print("\n" + "="*60)
    print("  全部任务执行完毕")
    print(f"  总耗时: {elapsed:.1f} 秒")
    print(f"  成功: {sum(1 for r in results if r['success'])}/{len(results)}")
    print("="*60)
    print("\n任务详情:")
    for i, r in enumerate(results, 1):
        status = "✓ 成功" if r["success"] else "✗ 失败"
        print(f"  {i}. {status} - {r['task']['desc']}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_file = os.path.join(LOG_DIR, f"{timestamp}_batch_summary.txt")
    with open(summary_file, "w", encoding="utf-8") as f:
        f.write("===== 批量任务汇总 =====\n")
        f.write(f"总耗时: {elapsed:.1f} 秒\n")
        f.write(f"成功: {sum(1 for r in results if r['success'])}/{len(results)}\n\n")
        for i, r in enumerate(results, 1):
            f.write(f"--- 任务 {i} ---\n")
            f.write(f"  描述: {r['task']['desc']}\n")
            f.write(f"  状态: {'成功' if r['success'] else '失败'}\n")
            f.write(f"  运行日志: {r['log']}\n\n")
    print(f"\n详细汇总已保存到: {summary_file}")


if __name__ == "__main__":
    try:
        interactive_menu()
    except KeyboardInterrupt:
        print("\n\n用户中断，退出程序")
    except Exception as e:
        print(f"\n[严重错误] {e}")
        import traceback
        traceback.print_exc()
