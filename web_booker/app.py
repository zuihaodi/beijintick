"""
变更记录（手动维护）:
- 2026-02-09 03:29 保留健康检查调度并统一任务通知/结果上报
- 2026-02-09 04:10 健康检查增加起始时间并在前端显示预计下次检查
"""

from flask import Flask, render_template, request, jsonify
import requests
import json
import urllib.parse
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timedelta
import traceback
import schedule
import time
import threading
import os
import hashlib

HEALTH_CHECK_NEXT_RUN = None

def normalize_time_str(value):
    if not value:
        return None
    if isinstance(value, str):
        value = value.strip()
        try:
            dt = datetime.strptime(value, "%H:%M")
            return dt.strftime("%H:%M")
        except ValueError:
            return None
    return None

# 定期健康检查的函数
def health_check():
    """
    定期检查获取场地状态是否正常，并发送短信通知。
    """
    phones = CONFIG.get('notification_phones') or []
    today = datetime.now().strftime("%Y-%m-%d")
    matrix_res = client.get_matrix(today)
    if "error" in matrix_res:
        err_msg = matrix_res["error"]
        log(f"❌ 健康检查失败: 获取场地状态异常: {err_msg}")
        if phones:
            task_manager.send_notification(f"⚠️ 健康检查失败：获取场地状态异常({err_msg})", phones=phones)
    else:
        log("✅ 健康检查通过：场地状态获取正常")

# 每隔一段时间执行健康检查
def schedule_health_check():
    """
    定时任务：按照配置的间隔时间运行健康检查。
    """
    # 清理已有的健康检查任务，避免重复调度
    schedule.clear("health_check")

    if not CONFIG.get('health_check_enabled', True):
        print("🛑 健康检查已关闭，不安排定时任务。")
        return

    check_interval = CONFIG.get('health_check_interval_min', 30)
    try:
        check_interval = float(check_interval)
    except (TypeError, ValueError):
        check_interval = 30.0
    if check_interval < 1:
        check_interval = 1
    start_time = CONFIG.get('health_check_start_time', '00:00')
    start_time = normalize_time_str(start_time) or '00:00'

    def compute_next_run():
        now = datetime.now()
        start_dt = datetime.strptime(
            f"{now.strftime('%Y-%m-%d')} {start_time}", "%Y-%m-%d %H:%M"
        )
        if now <= start_dt:
            return start_dt
        elapsed = (now - start_dt).total_seconds() / 60.0
        steps = int(elapsed // check_interval) + 1
        return start_dt + timedelta(minutes=steps * check_interval)

    def health_check_tick():
        global HEALTH_CHECK_NEXT_RUN
        if HEALTH_CHECK_NEXT_RUN is None:
            HEALTH_CHECK_NEXT_RUN = compute_next_run()
        if datetime.now() >= HEALTH_CHECK_NEXT_RUN:
            health_check()
            HEALTH_CHECK_NEXT_RUN = HEALTH_CHECK_NEXT_RUN + timedelta(minutes=check_interval)

    global HEALTH_CHECK_NEXT_RUN
    HEALTH_CHECK_NEXT_RUN = compute_next_run()
    schedule.every(1).minutes.do(health_check_tick).tag("health_check")
    print(
        f"📅 健康检查已安排，起始时间 {start_time}，每 {check_interval} 分钟执行一次."
    )


app = Flask(__name__)

# ================= 配置 =================
CONFIG = {
    "auth": {
        "token": "oy9Aj1fKpR3Yxwd6iV7VIlg3Vo-A", # 请确保有效
        "cookie": "JSESSIONID=FFE6C0633F33D9CE71354D0D1110AC0D",
        "card_index": "0873612446",
        "card_st_id": "289", 
        "shop_num": "1001"
    },
    "sms": {
        "user": "18600291931",
        "api_key": "6127d94d28a04c06a8f61b70eac79cc3"
    },
    "notification_phones": [],
    "retry_interval": 1.0,
    "aggressive_retry_interval": 1.0,
    "locked_retry_interval": 1.0,  # ✅ 新增：锁定状态重试间隔(秒)
    "locked_max_seconds": 60,  # ✅ 新增：锁定状态最多刷 N 秒
    # 🔍 新增：凭证健康检查
    "health_check_enabled": True,      # 是否开启自动健康检查
    "health_check_interval_min": 30.0, # 检查间隔（分钟）
    "health_check_start_time": "00:00", # 起始时间 (HH:MM)
}

CONFIG_FILE = "config.json"
LOG_BUFFER = []
MAX_LOG_SIZE = 500

def log(msg):
    """记录日志到内存缓冲区和控制台"""
    print(msg)
    timestamp = datetime.now().strftime("%H:%M:%S")
    LOG_BUFFER.append(f"[{timestamp}] {msg}")
    if len(LOG_BUFFER) > MAX_LOG_SIZE:
        LOG_BUFFER.pop(0)

if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
            if 'notification_phones' in saved:
                CONFIG['notification_phones'] = saved['notification_phones']
            if 'retry_interval' in saved:
                CONFIG['retry_interval'] = saved['retry_interval']
            if 'aggressive_retry_interval' in saved:
                CONFIG['aggressive_retry_interval'] = saved['aggressive_retry_interval']
            # ✅ 新增：锁定重试的两个配置
            if 'locked_retry_interval' in saved:
                CONFIG['locked_retry_interval'] = saved['locked_retry_interval']
            if 'locked_max_seconds' in saved:
                CONFIG['locked_max_seconds'] = saved['locked_max_seconds']
            if 'health_check_enabled' in saved:
                CONFIG['health_check_enabled'] = saved['health_check_enabled']
            if 'health_check_interval_min' in saved:
                CONFIG['health_check_interval_min'] = saved['health_check_interval_min']
            if 'health_check_start_time' in saved:
                CONFIG['health_check_start_time'] = normalize_time_str(saved['health_check_start_time']) or CONFIG['health_check_start_time']
            if 'auth' in saved:
                # 覆盖默认的 auth 配置
                CONFIG['auth'].update(saved['auth'])
    except Exception as e:
        print(f"加载配置失败: {e}")

TASKS_FILE = "tasks.json"

class ApiClient:
    def __init__(self):
        self.host = "gymvip.bfsu.edu.cn"
        self.headers = {
            "Host": self.host,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254162e) XWEB/18151 Flue",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"https://{self.host}",
            "Referer": f"https://{self.host}/easyserp/index.html",
            "Cookie": CONFIG["auth"]["cookie"]
        }
        self.token = CONFIG["auth"]["token"]
        self.session = requests.Session()

    def refresh_cookie(self):
        try:
            url = f"https://{self.host}/easyserp/index.html"
            resp = self.session.get(url, timeout=10, verify=False)
            jar = self.session.cookies
            jsid = jar.get("JSESSIONID")
            if not jsid:
                jsid = resp.cookies.get("JSESSIONID")
            if not jsid:
                return False, "未获取到JSESSIONID"
            cookie_str = f"JSESSIONID={jsid}"
            self.headers["Cookie"] = cookie_str
            CONFIG["auth"]["cookie"] = cookie_str
            try:
                saved = {}
                if os.path.exists(CONFIG_FILE):
                    try:
                        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                            saved = json.load(f)
                    except:
                        saved = {}
                if "auth" not in saved:
                    saved["auth"] = {}
                saved["auth"]["cookie"] = cookie_str
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(saved, f, ensure_ascii=False, indent=2)
            except:
                pass
            return True, "Cookie已刷新"
        except Exception as e:
            return False, str(e)

    def check_token(self):
        # 简单请求一次接口，看是否返回 token 失效相关的错误
        # 这里用获取矩阵接口测试，因为它只读且轻量
        today = datetime.now().strftime("%Y-%m-%d")
        res = self.get_matrix(today)
        
        # 假设接口返回 msg 包含 "token" 或 "登录" 字样代表失效
        # 具体根据实际抓包错误码调整
        if "error" in res:
            err = res["error"]
            # 扩展关键词：增加 "失效", "凭证", "-1"
            if any(k in err.lower() for k in ["token", "登录", "session", "失效", "凭证", "-1"]):
                return False, err
        return True, "Valid"

    def get_matrix(self, date_str):
        url = f"https://{self.host}/easyserpClient/place/getPlaceInfoByShortName"
        params = {
            "shopNum": CONFIG["auth"]["shop_num"],
            "dateymd": date_str,
            "shortName": "ymq",
            "token": self.token
        }
        try:
            # 抢票高峰期服务器响应慢，适当缩短超时以便快速重试，或者延长等待？
            # 考虑到 "Read timed out" (10s)，说明服务器卡死了。
            # 策略：保持 10s 超时，但在上层增加重试次数。
            resp = self.session.get(url, headers=self.headers, params=params, timeout=10, verify=False)
            
            try:
                data = resp.json()
            except json.JSONDecodeError:
                # 服务器可能返回了 HTML 错误页或空内容
                print(f"❌ [原始响应] 非JSON格式: {resp.text[:100]}...")
                return {"error": "服务器返回无效数据(可能是崩了)"}
            
            # 安全检查：确保 data 是字典
            if not isinstance(data, dict):
                print(f"❌ [API响应异常] 响应不是字典: {type(data)} - {data}")
                # 特殊处理 -1 (通常代表 Session/Token 失效)
                if data == -1 or str(data) == "-1":
                    return {"error": "会话失效(返回-1)，请更新Token和Cookie"}
                return {"error": f"API返回格式错误: {data}"}

            if data.get("msg") != "success":
                return {"error": data.get("msg")}
            
            raw_data = data.get('data')
            if isinstance(raw_data, str):
                try: raw_list = json.loads(raw_data)
                except: return {"error": "JSON解析失败"}
            else:
                raw_list = raw_data
                
            if isinstance(raw_list, dict):
                if 'placeArray' in raw_list:
                    raw_list = raw_list['placeArray']
                else:
                    return {"error": "无法找到场地列表"}

            matrix = {}
            all_times = set()
            
            # 添加调试日志，打印前几个数据的状态值，以便分析“全红”原因
            debug_states = []

            for place in raw_list:
                p_name = place['projectName']['shortname'] 
                p_num = p_name.replace('ymq', '').replace('mdb', '')
                
                status_map = {}
                for slot in place['projectInfo']:
                    t = slot['starttime']
                    s = slot['state']
                    all_times.add(t)
                    
                    if len(debug_states) < 5:
                        debug_states.append(f"{p_num}号{t}={s}")

                    # 1=可用, 其他=占用
                    # 根据调试日志修正：
                    # state=4: 似乎是“已占用”或“锁定” (全红时全是4)
                    # state=6: 似乎是“未开放”或“未来” (周五全是6)
                    # state=1: 偶尔出现，应该是“可用”
                    # state=0: 未知
                    
                    # 关键修改：
                    # 既然用户目的是“提前选中然后准时下单”，我们需要把“未开放”的状态也视为“可用(available)”
                    # 这样用户在前端就能选中并添加到愿望单了。
                    # 假设 6 是未开放但将来会开放。
                    # 假设 4 是已经被别人订了（不可选）。
                    # 假设 1 是当前就能买（可用）。
                    
                    # 策略：只要不是明确的“已预订(4?)”，都算 available？
                    # 或者更精确点：1(可用) 和 6(未开放) 都算 available。
                    # 暂时把 6 也加进去。

                    state_int = int(s)
                    if state_int == 1:
                        # 真正可以下单
                        status_map[t] = "available"
                    elif state_int == 6:
                        # 锁定未开放（当前日期 + 6 天那一列）
                        status_map[t] = "locked"
                    else:
                        # 已被别人订了 / 不可用
                        status_map[t] = "booked"

                matrix[p_num] = status_map
            
            print(f"🔍 [状态调试] 前5个样本状态: {debug_states}")
                
            sorted_places = sorted(matrix.keys(), key=lambda x: int(x) if x.isdigit() else 999)
            sorted_times = sorted(list(all_times))
            
            return {
                "places": sorted_places,
                "times": sorted_times,
                "matrix": matrix
            }
            
        except Exception as e:
            return {"error": str(e)}

    def submit_order(self, date_str, selected_items):
        """
        提交预订订单。
        关键修正：不再单纯依赖 reservationPlace 返回的 "msg":"success"，
        而是提交完成后重新拉取矩阵，确认选中场次的状态是否从 available 变为 booked。
        """
        url = f"https://{self.host}/easyserpClient/place/reservationPlace"

        results = []
        batch_size = 3

        # 将 items 分组，每组最多 3 个 (保守策略)
        for i in range(0, len(selected_items), batch_size):
            batch = selected_items[i:i + batch_size]
            print(f"📦 正在提交分批订单 ({i // batch_size + 1}): {batch}")

            field_info_list = []
            total_money = 0

            for item in batch:
                p_num = item["place"]
                start = item["time"]
                # 计算结束时间 & 按开始时间决定价格
                try:
                    st_obj = datetime.strptime(start, "%H:%M")
                    et_obj = st_obj + timedelta(hours=1)
                    end = et_obj.strftime("%H:%M")
                    # 简单价格规则：14:00 之前 80 元，之后 100 元
                    # 对应抓包中的 oldMoney 分布（10–13 点为 80，14 点以后为 100）
                    if st_obj.hour < 14:
                        price = 80
                    else:
                        price = 100
                except Exception:
                    # 异常时兜底：把结束时间和价格都设为常规晚间价格
                    end = "22:00"
                    price = 100

                # 根据场地号区分普通场 (1-14) 和木地板场 (15-17)
                try:
                    p_int = int(p_num)
                except (TypeError, ValueError):
                    p_int = None

                if p_int is not None and p_int >= 15:
                    # 木地板场：shortname 形如 mdb15，name 为 "木地板15"
                    place_short = f"mdb{p_num}"
                    place_name = f"木地板{p_num}"
                else:
                    # 普通羽毛球场：shortname 形如 ymq10，name 为 "羽毛球10"
                    place_short = f"ymq{p_num}"
                    place_name = f"羽毛球{p_num}"

                info = {
                    "day": date_str,
                    "oldMoney": price,
                    "startTime": start,
                    "endTime": end,
                    "placeShortName": place_short,
                    "name": place_name,
                    "stageTypeShortName": "ymq",
                    "newMoney": price,
                }
                field_info_list.append(info)
                total_money += price

            info_str = urllib.parse.quote(
                json.dumps(field_info_list, separators=(",", ":"), ensure_ascii=False)
            )
            type_encoded = urllib.parse.quote("羽毛球")

            body = (
                f"token={self.token}&"
                f"shopNum={CONFIG['auth']['shop_num']}&"
                f"fieldinfo={info_str}&"
                f"cardStId={CONFIG['auth']['card_st_id']}&"
                f"oldTotal={total_money}.00&"
                f"cardPayType=0&"
                f"type={type_encoded}&"
                f"offerId=&"
                f"offerType=&"
                f"total={total_money}.00&"
                f"premerother=&"
                f"cardIndex={CONFIG['auth']['card_index']}"
            )

            try:
                resp = self.session.post(
                    url, headers=self.headers, data=body, timeout=10, verify=False
                )

                # 解析响应并输出调试
                try:
                    resp_data = resp.json()
                except ValueError:
                    resp_data = None

                print(
                    f"📨 [submit_order调试] 批次 {i // batch_size + 1} 响应: {resp.text}"
                )

                if resp_data and resp_data.get("msg") == "success":
                    results.append({"status": "success"})
                else:
                    fail_msg = None
                    if isinstance(resp_data, dict):
                        fail_msg = resp_data.get("data") or resp_data.get("msg")
                    if not fail_msg:
                        fail_msg = resp.text
                    results.append({"status": "fail", "msg": fail_msg})
            except Exception as e:
                results.append({"status": "error", "msg": str(e)})

            # 稍作停顿防止并发过快
            time.sleep(CONFIG.get("retry_interval", 0.5))

        # ---------- 下单后验证 ----------
        verify_success_count = None
        try:
            verify = self.get_matrix(date_str)
            if isinstance(verify, dict) and not verify.get("error"):
                v_matrix = verify["matrix"]
                verify_states = []
                booked_map = []

                for item in selected_items:
                    p = str(item["place"])
                    t = item["time"]
                    status = v_matrix.get(p, {}).get(t, "N/A")
                    verify_states.append(f"{p}号{t}={status}")
                    booked_map.append(status == "booked")

                print(f"🧾 [提交后验证调试] 选中场次最新状态: {verify_states}")
                verify_success_count = sum(1 for ok in booked_map if ok)
            else:
                print(
                    f"🧾 [提交后验证调试] 获取矩阵失败: "
                    f"{verify.get('error') if isinstance(verify, dict) else verify}"
                )
        except Exception as e:
            print(f"🧾 [提交后验证调试] 异常: {e}")

        # ---------- 汇总结果 ----------
        # 1) 接口返回层面的成功批次数
        api_success_count = sum(1 for r in results if r.get("status") == "success")

        # 2) 真实已被占用的场次数量（如果验证成功）
        if verify_success_count is not None:
            success_count = verify_success_count
        else:
            success_count = api_success_count

        # 3) 本次计划总共尝试下单的场次数
        total_items = len(selected_items) if selected_items else 0

        # 兼容老逻辑：如果 selected_items 为空（理论上不应该），
        # 退回到按批次数统计，防止 denominator 为 0。
        denominator = total_items or len(results)

        if denominator == 0:
            msg = "没有生成任何下单项目，请检查配置或场地状态。"
            return {"status": "fail", "msg": msg}

        if success_count == denominator:
            return {"status": "success", "msg": "全部下单成功"}
        elif success_count > 0:
            return {
                "status": "partial",
                "msg": f"部分成功 ({success_count}/{denominator})",
            }
        else:
            # 特殊情况：接口返回 success，但验证结果全是 available
            if api_success_count > 0 and verify_success_count == 0:
                msg = "接口返回 success，但场地状态未变化，请在微信小程序确认或检查参数。"
            else:
                first_fail = results[0] if results else {"msg": "无数据"}
                msg = first_fail.get("msg")
            return {"status": "fail", "msg": msg}

    def x_submit_order_old(self, date_str, selected_items):
        pass

client = ApiClient()

# ================= 任务调度系统 =================

class TaskManager:
    def __init__(self):
        self.tasks = []
        self.load_tasks()
        
    def load_tasks(self):
        if os.path.exists(TASKS_FILE):
            try:
                with open(TASKS_FILE, 'r', encoding='utf-8') as f:
                    self.tasks = json.load(f)
            except:
                self.tasks = []
                
    def save_tasks(self):
        with open(TASKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.tasks, f, ensure_ascii=False, indent=2)
            
    def add_task(self, task):
        # task: {id, type='daily'|'weekly', run_time='08:00', target_day_offset=2, items=[...]}
        task['id'] = int(time.time() * 1000)
        self.tasks.append(task)
        self.save_tasks()
        self.refresh_schedule()

    def delete_task(self, task_id, refresh=True):
        self.tasks = [t for t in self.tasks if t['id'] != int(task_id)]
        self.save_tasks()
        if refresh:
            self.refresh_schedule()

    def send_notification(self, content, phones=None):
        """
        发送短信通知：
        - phones 不为 None 时，优先使用传入的号码（任务级别）
        - 否则退回到全局 CONFIG['notification_phones']
        """
        if phones is None:
            phones = CONFIG.get('notification_phones', [])

        # 归一化手机号：允许字符串/列表混用
        if isinstance(phones, str):
            phones = [p.strip() for p in phones.split(',') if p.strip()]
        elif isinstance(phones, list):
            phones = [str(p).strip() for p in phones if str(p).strip()]

        if not phones:
            log(f"⚠️ 未配置短信手机号，通知内容未发送: {content}")
            return  # 没有号码就直接返回

        log(f"📧 正在发送短信通知给: {phones}")
        try:
            u = CONFIG['sms']['user']
            p = CONFIG['sms']['api_key']

            error_map = {
                '0': '发送成功',
                '30': '密码错误',
                '40': '账号不存在',
                '41': '余额不足',
                '42': '帐号过期',
                '43': 'IP地址限制',
                '50': '内容含有敏感词',
                '51': '手机号码不正确'
            }

            m = ",".join(phones)
            c = f"【数数云端】{content}"

            params = {
                "u": u,
                "p": p,
                "m": m,
                "c": c
            }

            resp = requests.get("https://api.smsbao.com/sms", params=params, timeout=10)

            code = resp.text
            msg = error_map.get(code, f"未知错误({code})")
            log(f"📧 短信接口返回: [{code}] {msg}")

            if code != '0':
                log(f"⚠️ 短信发送异常: {msg}")
                return False, msg
            return True, "发送成功"

        except Exception as e:
            log(f"❌ 短信发送异常: {e}")
            return False, str(e)

    def execute_task(self, task):
        log(f"⏰ [自动任务] 开始执行任务: {task.get('id')}")

        # 每个任务自己配置的通知手机号（列表），用于“下单成功”类通知
        task_phones = task.get('notification_phones') or None
        task_id = task.get('id')
        last_fail_reason = None

        def build_date_display(date_str):
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                weekday_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                weekday_label = weekday_map[dt.weekday()]
                return dt.strftime("%Y-%m-%d") + f"（{weekday_label}）"
            except Exception:
                return date_str

        def notify_task_result(success, message, items=None, date_str=None):
            prefix = "【预订成功】" if success else "【预订失败】"
            details = message
            if date_str:
                details = f"{build_date_display(date_str)} {message}"
            self.send_notification(f"{prefix}{details}", phones=task_phones)

        # 0. 先检查 token 是否有效（只记录日志，不立刻报警）
        #    以“获取场地状态异常”为准触发短信提醒，避免误报
        is_valid, token_msg = client.check_token()
        if not is_valid:
            log(f"⚠️ Token 可能已失效，但继续尝试获取场地状态: {token_msg}")

        # 1. 计算目标日期
        # 新增 target_mode / target_date 支持：
        # - target_mode == 'fixed' 且有 target_date 时，直接使用该日期
        # - 否则退回到旧逻辑：使用 target_day_offset 延后 N 天
        target_mode = task.get('target_mode', 'offset')
        if target_mode == 'fixed' and task.get('target_date'):
            target_date = str(task['target_date'])
        else:
            # 兼容：老任务可能没有 target_day_offset 字段，默认按 0 天处理
            offset_days = int(task.get('target_day_offset', 0))
            target_date = (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d")

        config = task.get('config')

        # 2. 安全检查：确保 config 是 dict
        if not isinstance(config, dict):
            if config is not None:
                log(f"⚠️ 警告: 任务 {task.get('id')} 的 config 字段类型异常 ({type(config)})，已重置为空字典")
            config = {}

        # 3. 旧版兼容：没有新配置时走最早的 items 逻辑
        if not config and 'items' in task:
            res = client.submit_order(target_date, task['items'])
            status = res.get("status")
            if status in ("success", "partial"):
                msg = "全部成功" if status == "success" else "部分成功"
                notify_task_result(True, f"下单完成：{msg}（{status}）", items=task['items'], date_str=target_date)
            else:
                notify_task_result(False, f"下单失败：{res.get('msg')}", items=task['items'], date_str=target_date)
            return

        # 4. 这次任务真正关心的 (场地, 时间) 组合，用来判断是否还在“锁定未开放”阶段
        def enumerate_candidate_pairs(cfg):
            pairs = set()
            mode = cfg.get('mode', 'normal')
            target_times = cfg.get('target_times', [])

            if mode == 'normal':
                for p in cfg.get('candidate_places', []):
                    for t in target_times:
                        pairs.add((str(p), t))

            elif mode == 'priority':
                sequences = cfg.get('priority_sequences', [])
                for t in target_times:
                    for seq in sequences:
                        for p in seq:
                            pairs.add((str(p), t))

            elif mode == 'time_priority':
                candidate_places = [str(p) for p in cfg.get('candidate_places', [])]
                if not candidate_places:
                    candidate_places = [str(i) for i in range(1, 16)]
                sequences = cfg.get('priority_time_sequences', []) or [[t] for t in target_times]
                for seq in sequences:
                    for t in seq:
                        for p in candidate_places:
                            pairs.add((p, t))
            return pairs

        candidate_pairs = enumerate_candidate_pairs(config)

        # === 智能抢票核心逻辑 ===
        retry_interval = CONFIG.get('retry_interval', 0.5)
        aggressive_retry_interval = CONFIG.get('aggressive_retry_interval', 0.3)

        # 新增：锁定状态下的重试间隔 & 最多等待时间
        locked_retry_interval = CONFIG.get('locked_retry_interval', retry_interval)
        locked_max_seconds = CONFIG.get('locked_max_seconds', 60)

        # 记录进入「锁定等待模式」的起始时间，用于统计已等待多久
        locked_mode_started_at = None

        attempt = 0
        while True:

            # 允许在运行过程中通过 config.json 调整重试速度
            retry_interval = CONFIG.get('retry_interval', retry_interval)
            aggressive_retry_interval = CONFIG.get('aggressive_retry_interval', aggressive_retry_interval)
            locked_retry_interval = CONFIG.get('locked_retry_interval', locked_retry_interval)
            locked_max_seconds = CONFIG.get('locked_max_seconds', locked_max_seconds)

            attempt += 1
            log(f"🔄 第 {attempt} 轮无限尝试...喵")

            # 1. 获取最新场地状态
            matrix_res = client.get_matrix(target_date)

            # 1.1 错误处理（服务器崩了 / token 失效等）
            if "error" in matrix_res:
                err_msg = matrix_res["error"]
                log(f"获取状态失败: {err_msg} 喵")

                # 服务器直接 404 / 非 JSON，说明挂了 —— 死磕模式
                if "非JSON格式" in err_msg or "404" in err_msg or "无效数据" in err_msg:
                    log(f"⚠️ 检测到服务器异常，启用高频重试 ({aggressive_retry_interval}s)")
                    time.sleep(aggressive_retry_interval)
                    continue

                # 会话 / 凭证失效，这种重试也没用，直接报警退出
                if "失效" in err_msg or "凭证" in err_msg or "token" in err_msg.lower():
                    log(f"❌ 严重错误: {err_msg}，任务终止。")
                    notify_task_result(False, f"登录状态/Token 失效({err_msg})，请尽快处理！", date_str=target_date)
                    return

                # 普通错误：按普通间隔重试
                time.sleep(retry_interval)
                continue

            # 1.2 正常拿到矩阵
            matrix = matrix_res.get("matrix", {})
            target_times = config.get('target_times', [])

            # 2. 判断当前目标是否还有「锁定未开放」的场次
            locked_exists = False
            for p, t in candidate_pairs:
                state = matrix.get(str(p), {}).get(t)
                if state == "locked":
                    locked_exists = True
                    break

            # 3. 根据不同模式生成最终下单列表 final_items
            final_items: list[dict] = []

            # --- 模式 A: 场地优先优先级序列 (priority) ---
            if config.get('mode') == 'priority':
                sequences = config.get('priority_sequences', [])  # 例如 [["6","7"],["8","9"]]
                target_count = int(config.get('target_count', 2))
                allow_partial = config.get('allow_partial', True)

                # 3.1 第一轮：优先尝试完整序列
                for time_slot in target_times:
                    if len(final_items) >= target_count:
                        break

                    for seq in sequences:
                        if len(final_items) >= target_count:
                            break

                        # 如果这一组长度 > 当前剩余需求，跳过
                        if len(seq) > (target_count - len(final_items)):
                            continue

                        all_avail = True
                        # 这组里的每个场地在该时间都必须 available
                        for p in seq:
                            if p not in matrix or matrix[p].get(time_slot) != "available":
                                all_avail = False
                                break

                        # 避免重复加入相同 (场地, 时间)
                        if all_avail:
                            for p in seq:
                                for item in final_items:
                                    if item['place'] == str(p) and item['time'] == time_slot:
                                        all_avail = False
                                        break

                        if all_avail:
                            log(f"   -> 🎯 [优先级-整] 命中完整组合: {seq} @ {time_slot}")
                            for p in seq:
                                final_items.append({"place": str(p), "time": time_slot})

                # 3.2 第二轮：散单补齐
                if allow_partial and len(final_items) < target_count:
                    log(f"   -> ⚠️ [优先级-散] 完整组合不足，开始散单填充 (目标{target_count}, 已有{len(final_items)})")
                    for time_slot in target_times:
                        if len(final_items) >= target_count:
                            break
                        for seq in sequences:
                            if len(final_items) >= target_count:
                                break
                            for p in seq:
                                if p in matrix and matrix[p].get(time_slot) == "available":
                                    is_picked = False
                                    for item in final_items:
                                        if item['place'] == str(p) and item['time'] == time_slot:
                                            is_picked = True
                                            break
                                    if not is_picked:
                                        log(f"   -> 🧩 [优先级-散] 捡漏: {p}号 @ {time_slot}")
                                        final_items.append({"place": str(p), "time": time_slot})
                                        if len(final_items) >= target_count:
                                            break

            # --- 模式 B: 时间优先 (time_priority) ---
            elif config.get('mode') == 'time_priority':
                sequences = config.get('priority_time_sequences', []) or [[t] for t in target_times]
                candidate_places = [str(p) for p in config.get('candidate_places', [])]
                # 不选场地 == 默认全场参与
                if not candidate_places:
                    candidate_places = [str(i) for i in range(1, 16)]

                target_count = int(config.get('target_count', 2))
                allow_partial = config.get('allow_partial', True)

                # 3.1 优先尝试整段时间序列（比如 14-16 连续两小时）
                for seq in sequences:
                    if len(final_items) >= target_count:
                        break

                    for p in candidate_places:
                        if len(final_items) >= target_count:
                            break

                        ok = True
                        for t in seq:
                            if p not in matrix or matrix[p].get(t) != "available":
                                ok = False
                                break
                        if not ok:
                            continue

                        # 避免重复
                        already = False
                        for t in seq:
                            for item in final_items:
                                if item["place"] == p and item["time"] == t:
                                    already = True
                                    break
                            if already:
                                break
                        if already:
                            continue

                        log(f"   -> 🎯 [时间优先-整] {p}号 命中时间段 {seq}")
                        for t in seq:
                            final_items.append({"place": p, "time": t})
                        if len(final_items) >= target_count:
                            break

                # 3.2 如果还不够，并且允许散单，则按时间逐个捡漏
                if allow_partial and len(final_items) < target_count:
                    for t in target_times:
                        if len(final_items) >= target_count:
                            break
                        for p in candidate_places:
                            if len(final_items) >= target_count:
                                break
                            if p in matrix and matrix[p].get(t) == "available":
                                already = False
                                for item in final_items:
                                    if item["place"] == p and item["time"] == t:
                                        already = True
                                        break
                                if not already:
                                    final_items.append({"place": p, "time": t})
                                    log(f"   -> 🧩 [时间优先-散] 捡漏: {p}号 @ {t}")

            # --- 模式 C: 普通 / 智能连号 (normal) ---
            else:
                if 'candidate_places' not in config:
                    log(f"❌ 任务配置错误: 非优先级模式必须包含 candidate_places")
                    notify_task_result(False, "任务配置错误：缺少 candidate_places。", date_str=target_date)
                    return

                candidate_places = [str(p) for p in config['candidate_places']]
                target_courts = int(config.get('target_count', 2))  # 目标是“几块场地”
                smart_mode = config.get('smart_continuous', False)

                if target_courts <= 0:
                    log("⚠️ 目标场地数量 target_count <= 0，跳过本轮。")
                else:
                    # 先找出“在所有目标时间段都可用”的候选场地
                    available_courts: list[int] = []
                    for p in candidate_places:
                        p_str = str(p)
                        ok = True
                        for t in target_times:
                            if p_str not in matrix or matrix[p_str].get(t) != "available":
                                ok = False
                                break
                        if ok:
                            available_courts.append(int(p))

                    if not available_courts:
                        log("⚠️ 当前没有同时满足所有时间段的候选场地。")
                    else:
                        available_courts.sort()
                        need = min(target_courts, len(available_courts))

                        selected_courts: list[int] = []

                        if smart_mode and len(available_courts) > 1:
                            # 智能连号：优先选择一段连续场地
                            best_run: list[int] | None = None
                            best_len = 0
                            i = 0
                            while i < len(available_courts):
                                j = i
                                while j + 1 < len(available_courts) and \
                                        available_courts[j + 1] == available_courts[j] + 1:
                                    j += 1
                                run = available_courts[i: j + 1]
                                if len(run) > best_len:
                                    best_len = len(run)
                                    best_run = run
                                i = j + 1

                            if best_run:
                                selected_courts = best_run[:need]

                        # 普通模式或者智能模式没找到合适连号
                        if not selected_courts:
                            selected_courts = available_courts[:need]

                        # 为每块选中的场地添加所有时间段
                        for p_int in selected_courts:
                            p_str = str(p_int)
                            for t in target_times:
                                final_items.append({"place": p_str, "time": t})

            # 4. 提交订单
            if final_items:
                log(f"正在提交分批订单: {final_items}")
                res = client.submit_order(target_date, final_items)
                log(f"[submit_order调试] 批次响应: {res}")

                status = res.get("status")
                if status in ("success", "partial"):
                    msg = "全部成功" if status == "success" else "部分成功"
                    log(f"✅ 下单完成: {msg} ({status})")

                    # 发通知短信
                    try:
                        notify_task_result(
                            True,
                            f"已预订",
                            items=final_items,
                            date_str=target_date,
                        )
                    except Exception as e:
                        log(f"构建短信内容失败: {e}")

                    return
                else:
                    log(f"❌ 下单失败: {res.get('msg')}")
                    last_fail_reason = res.get('msg') or "下单失败"

            # 5. 根据 locked 状态决定是否继续死磕（使用锁定配置 + 最多刷 N 秒保护）
            if locked_exists:
                now_ts = time.time()

                # 第一次发现 locked，开始计时
                if locked_mode_started_at is None:
                    locked_mode_started_at = now_ts

                elapsed = now_ts - locked_mode_started_at

                # 超过配置的最大等待时间 -> 放弃本次任务
                if elapsed >= locked_max_seconds:
                    log(
                        f"⏳ 已连续等待『锁定未开放』状态约 {int(elapsed)} 秒，"
                        f"达到上限 {locked_max_seconds}s，本次任务结束。"
                    )
                    fail_msg = "锁定未开放等待超时，任务结束。"
                    if last_fail_reason:
                        fail_msg = f"{fail_msg} 失败原因：{last_fail_reason}"
                    notify_task_result(False, fail_msg, date_str=target_date)
                    return

                # 仍在允许范围内，按锁定间隔继续轮询
                log(
                    f"⏳ 当前目标场地处于『锁定未开放』状态，继续等待下一轮..."
                    f" (已等待 {int(elapsed)} 秒 / 上限 {locked_max_seconds}s)"
                )
                time.sleep(locked_retry_interval)
                continue
            else:
                # 一旦不再是 locked（要么 available 被抢完，要么状态变 booked），重置计时并结束
                locked_mode_started_at = None
                log("🙈 目标场地已经开放但没有可用组合(大概率被别人抢完了)，本次任务结束。")
                fail_msg = "目标场地已开放但无可用组合，可能已被抢完。"
                if last_fail_reason:
                    fail_msg = f"{fail_msg} 失败原因：{last_fail_reason}"
                notify_task_result(False, fail_msg, date_str=target_date)
                return

        # print(" 所有重试均失败，放弃。")

    def refresh_schedule(self):
        schedule.clear("task")
        print(f"🔄 [调度器] 正在刷新任务列表 (共 {len(self.tasks)} 个)...")

        # 内部工具函数：支持单次任务执行完后自动删除自身
        def make_job(t, is_once=False):
            def _job():
                print(f"⏰ [调度器] 触发任务 ID: {t['id']}")
                self.execute_task(t)
                if is_once:
                    print(f"✅ 单次任务 {t['id']} 执行完成，自动从任务列表中删除")
                    # 不再 refresh_schedule，避免在调度循环里频繁清空重建
                    self.delete_task(t['id'], refresh=False)
                    # 告诉 schedule 取消当前 job
                    return schedule.CancelJob

            return _job

        for task in self.tasks:
            run_time = task['run_time']
            # 确保时间格式是 HH:mm:ss (有的浏览器可能只返回 HH:mm)
            if len(run_time) == 5:
                run_time += ":00"

            t_type = task.get('type', 'daily')

            try:
                if t_type == 'daily':
                    schedule.every().day.at(run_time).do(make_job(task, is_once=False)).tag("task")
                    print(f"   -> 已添加每日任务: {run_time}")
                elif t_type == 'weekly':
                    days = [
                        schedule.every().monday,
                        schedule.every().tuesday,
                        schedule.every().wednesday,
                        schedule.every().thursday,
                        schedule.every().friday,
                        schedule.every().saturday,
                        schedule.every().sunday,
                    ]
                    wd = int(task['weekly_day'])
                    days[wd].at(run_time).do(make_job(task, is_once=False)).tag("task")
                    print(f"   -> 已添加每周任务: 周{['一', '二', '三', '四', '五', '六', '日'][wd]} {run_time}")
                elif t_type == 'once':
                    # 单次任务：到点执行一次，然后自动从任务列表和调度器中移除
                    schedule.every().day.at(run_time).do(make_job(task, is_once=True)).tag("task")
                    print(f"   -> 已添加单次任务: {run_time}（执行一次后自动删除）")
            except Exception as e:
                print(f"❌ 添加任务失败: {e}")


task_manager = TaskManager()

def run_scheduler():
    print("🚀 [后台] 任务调度线程已启动...")
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print(f"⚠️ 调度执行出错: {e}")
            print(traceback.format_exc())
        time.sleep(1)

# 启动后台线程
threading.Thread(target=run_scheduler, daemon=True).start()

# ================= 路由 =================

@app.route('/')
def index():
    dates = []
    today = datetime.now()
    weekdays = ["周一","周二","周三","周四","周五","周六","周日"]
    # 显示未来 14 天 (2周) 以支持更远的预定
    for i in range(14):
        d = today + timedelta(days=i)
        dates.append({
            "val": d.strftime("%Y-%m-%d"),
            "weekday": weekdays[d.weekday()],
            "date_only": d.strftime("%m-%d")
        })
    return render_template('index.html', dates=dates, tasks=task_manager.tasks)

@app.route('/api/matrix')
def api_matrix():
    date = request.args.get('date')
    return jsonify(client.get_matrix(date))

@app.route('/api/time')
def api_time():
    return jsonify({"timestamp": datetime.now().timestamp()})

@app.route('/api/book', methods=['POST'])
def api_book():
    data = request.json
    date = data.get('date')
    items = data.get('items')
    res = client.submit_order(date, items)
    
    # 增加手动预订后的短信通知
    # 只要状态不是 fail，就发送通知（success 或 partial）
    if res.get('status') in ['success', 'partial']:
        print(f"📧 [调试] 准备发送手动预订通知，状态: {res.get('status')}")
        try:
            status_desc = "已预订成功！" if res['status'] == 'success' else "已预订部分成功！"
            detail_msg = f"{status_desc}日期{date}: "
            items_str = []
            for item in items:
                items_str.append(f"{item['place']}号场({item['time']})")
            detail_msg += ",".join(items_str)
            detail_msg += "。"
            
            # 强制检查一次手机号配置
            phones = CONFIG.get('notification_phones', [])
            if not phones:
                print(f"⚠️ [调试] 此时内存中 notification_phones 为空，尝试重新加载...")
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                        saved = json.load(f)
                        CONFIG['notification_phones'] = saved.get('notification_phones', [])
                        print(f"⚠️ [调试] 重新加载后手机号: {CONFIG['notification_phones']}")
            
            task_manager.send_notification(detail_msg)
        except Exception as e:
            print(f"手动预订通知发送失败: {e}")
            print(traceback.format_exc())
            
    else:
        print(f"📧 [调试] 预订状态为 {res.get('status')}，不发送通知。返回msg: {res.get('msg')}")
        
    return jsonify(res)

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(CONFIG)

@app.route('/api/config', methods=['POST'])
def update_config():
    """
    更新全局配置：
    - notification_phones：全局报警手机号（列表，可以填 0~N 个）
    - retry_interval：普通重试间隔
    - aggressive_retry_interval：死磕模式重试间隔
    - locked_retry_interval：锁定状态重试间隔
    - locked_max_seconds：锁定状态最多刷 N 秒
    - health_check_enabled: 健康检查是否开启
    - health_check_interval_min: 健康检查间隔（分钟）
    - health_check_start_time: 健康检查起始时间（HH:MM）
    """
    try:
        data = request.json or {}

        # 读取旧配置，保证 auth / sms 等字段不会丢
        saved = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f) or {}
            except Exception as e:
                print(f"加载配置失败: {e}")
                saved = {}

        # 确保 auth / sms 结构存在（不改动它们）
        if 'auth' not in saved:
            saved['auth'] = CONFIG.get('auth', {}).copy()
        if 'sms' not in saved:
            saved['sms'] = CONFIG.get('sms', {}).copy()

        # 小工具：更新一个浮点字段（带最小值与默认值）
        def _update_float_field(field, min_value, default_value):
            if field not in data:
                return
            try:
                val = float(data[field])
            except (TypeError, ValueError):
                val = default_value
            if val < min_value:
                val = min_value
            CONFIG[field] = val
            saved[field] = val

        # 1) 全局报警手机号
        if 'notification_phones' in data:
            phones = data['notification_phones'] or []
            if isinstance(phones, str):
                phones = [p.strip() for p in phones.split(',') if p.strip()]
            elif isinstance(phones, list):
                phones = [str(p).strip() for p in phones if str(p).strip()]
            else:
                phones = []
            CONFIG['notification_phones'] = phones
            saved['notification_phones'] = phones

        # 2) 各类重试 / 限制配置
        _update_float_field('retry_interval', 0.1, CONFIG.get('retry_interval', 1.0))
        _update_float_field('aggressive_retry_interval', 0.1, CONFIG.get('aggressive_retry_interval', 0.3))
        _update_float_field('locked_retry_interval', 0.1, CONFIG.get('locked_retry_interval', 1.0))
        _update_float_field('locked_max_seconds', 1.0, CONFIG.get('locked_max_seconds', 60.0))
        _update_float_field('health_check_interval_min', 1.0, CONFIG.get('health_check_interval_min', 30.0))

        if 'health_check_start_time' in data:
            time_str = normalize_time_str(data['health_check_start_time'])
            if time_str:
                CONFIG['health_check_start_time'] = time_str
                saved['health_check_start_time'] = time_str

        # 3) 健康检查开关（勾选 / 取消）
        if 'health_check_enabled' in data:
            val = data['health_check_enabled']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['health_check_enabled'] = enabled
            saved['health_check_enabled'] = enabled

        # 4) 写回 config.json
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(saved, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入配置文件失败: {e}")
            # 即使写文件失败，内存中的 CONFIG 已经更新了

        # 5) 重新安排健康检查（应用新的开关/间隔）
        schedule_health_check()

        return jsonify({"status": "success"})

    except Exception as e:
        print(f"更新配置时异常: {e}")
        return jsonify({"status": "error", "msg": str(e)})


@app.route('/api/config/auth', methods=['POST'])
def update_auth():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "msg": "请求体为空"})
            
        if 'token' in data and 'cookie' in data:
            # 去除首尾空格
            token = data['token'].strip()
            cookie = data['cookie'].strip()
            
            CONFIG['auth']['token'] = token
            CONFIG['auth']['cookie'] = cookie
            
            # 更新 client 实例
            client.token = token
            client.headers['Cookie'] = cookie
            
            # 持久化保存
            try:
                saved = {}
                if os.path.exists(CONFIG_FILE):
                    try:
                        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                            saved = json.load(f)
                    except: pass
                
                # 确保 auth 结构存在
                if 'auth' not in saved: saved['auth'] = {}
                
                saved['auth']['token'] = token
                saved['auth']['cookie'] = cookie
                # 保留其他 auth 字段 (如 shop_num)
                saved['auth']['card_index'] = CONFIG['auth'].get('card_index', '')
                saved['auth']['card_st_id'] = CONFIG['auth'].get('card_st_id', '')
                saved['auth']['shop_num'] = CONFIG['auth'].get('shop_num', '')

                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(saved, f, ensure_ascii=False, indent=2)
                    
            except Exception as e:
                print(f"保存Auth配置失败: {e}")
                # 即使保存失败，内存更新成功也算成功，但记录日志
                
            return jsonify({"status": "success", "msg": "凭证已更新"})
        return jsonify({"status": "error", "msg": "Token或Cookie缺失"})
    except Exception as e:
        print(f"Update Auth Error: {e}")
        return jsonify({"status": "error", "msg": f"服务器内部错误: {str(e)}"})

@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    return jsonify(task_manager.tasks)

@app.route('/api/tasks', methods=['POST'])
def add_task():
    data = request.json
    task_manager.add_task(data)
    return jsonify({"status": "success"})

@app.route('/api/tasks/<task_id>', methods=['DELETE'])
def del_task(task_id):
    task_manager.delete_task(task_id)
    return jsonify({"status": "success"})

@app.route('/api/tasks/<task_id>/run', methods=['POST'])
def run_task_now(task_id):
    # Find task
    task = next((t for t in task_manager.tasks if str(t['id']) == str(task_id)), None)
    if task:
        # Run in a separate thread to avoid blocking the response
        threading.Thread(target=task_manager.execute_task, args=(task,)).start()
    return jsonify({"status": "success", "msg": "Task started"})
    return jsonify({"status": "error", "msg": "Task not found"}), 404

@app.route('/api/config/check-token', methods=['POST'])
def check_token_api():
    valid, msg = client.check_token()
    if valid:
        return jsonify({"status": "success", "msg": "Token 有效喵！"})
    else:
        # 如果失效，尝试发短信提醒（如果配置了手机号）
        task_manager.send_notification(f"警告：您的 Token 可能已失效 ({msg})，请及时更新喵！")
        return jsonify({"status": "error", "msg": f"Token 失效: {msg} 喵"})

@app.route('/api/config/refresh-cookie', methods=['POST'])
def refresh_cookie_api():
    ok, msg = client.refresh_cookie()
    if ok:
        return jsonify({"status": "success", "msg": msg, "cookie": CONFIG["auth"]["cookie"]})
    return jsonify({"status": "error", "msg": msg})

@app.route('/api/config/test-sms', methods=['POST'])
def test_sms():
    data = request.json
    phones = data.get('phones', [])
    if not phones: return jsonify({"status": "error", "msg": "请输入手机号喵"})
    
    # 临时覆盖配置以测试
    original_phones = CONFIG.get('notification_phones', [])
    CONFIG['notification_phones'] = phones
    
    try:
        # 尝试发送
        success, msg = task_manager.send_notification("这是一条测试短信，收到代表配置成功喵！")
        if success:
            return jsonify({"status": "success", "msg": "接口调用成功(返回码0)，请留意手机短信喵"})
        else:
            return jsonify({"status": "error", "msg": f"发送失败: {msg} 喵"})
    except Exception as e:
        print(f"测试接口异常: {e}")
        return jsonify({"status": "error", "msg": f"服务端异常: {str(e)}"})
    finally:
        # 恢复配置
        CONFIG['notification_phones'] = original_phones

@app.route('/api/logs', methods=['GET'])
def get_logs():
    return jsonify(LOG_BUFFER)

if __name__ == "__main__":
    # 首次启动刷新调度
    task_manager.refresh_schedule()

    # 启动健康检查调度（如果启用）
    schedule_health_check()

    print("🚀 服务已启动，访问 http://127.0.0.1:5000")
    print("📋 已加载测试接口: /api/config/test-sms")
    app.run(debug=True, port=5000, use_reloader=False)  # 关闭 reloader 防止线程重复启动
