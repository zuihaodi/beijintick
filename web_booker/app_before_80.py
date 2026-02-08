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
    "retry_interval": 0.5,
    "aggressive_retry_interval": 0.3
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
                    
                    if int(s) == 1 or int(s) == 6:
                         status_map[t] = "available"
                    else:
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
                try:
                    st_obj = datetime.strptime(start, "%H:%M")
                    et_obj = st_obj + timedelta(hours=1)
                    end = et_obj.strftime("%H:%M")
                except Exception:
                    end = "22:00"

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
                    "oldMoney": 100,
                    "startTime": start,
                    "endTime": end,
                    "placeShortName": place_short,
                    "name": place_name,
                    "stageTypeShortName": "ymq",
                    "newMoney": 100,
                }
                field_info_list.append(info)
                total_money += 100

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
        # 接口返回层面的成功数
        api_success_count = sum(1 for r in results if r.get("status") == "success")

        # 如果验证成功拿到了状态，以“实际已占用的数量”为准
        if verify_success_count is not None:
            success_count = verify_success_count
        else:
            success_count = api_success_count

        total_batches = len(results) if results else 0

        if success_count == total_batches and success_count > 0:
            return {"status": "success", "msg": "全部下单成功"}
        elif success_count > 0:
            return {
                "status": "partial",
                "msg": f"部分成功 ({success_count}/{total_batches})",
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
        
    def delete_task(self, task_id):
        self.tasks = [t for t in self.tasks if t['id'] != int(task_id)]
        self.save_tasks()
        self.refresh_schedule()

    def send_notification(self, content):
        phones = CONFIG.get('notification_phones', [])
        if not phones: return
        
        print(f"📧 正在发送短信通知给: {phones}")
        try:
            u = CONFIG['sms']['user']
            p = CONFIG['sms']['api_key']
            
            # 短信宝错误码映射
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

            # 建议测试时只发一个号码，避免被判定为群发需审核
            # 这里为了兼容多号码，还是拼在一起，但用户需知悉可能延迟
            m = ",".join(phones)
            c = f"【抢票助手】{content}" 
            
            # 使用 params 让 requests 自动处理编码，避免 URL 拼接错误
            params = {
                "u": u,
                "p": p,
                "m": m,
                "c": c
            }
            
            # 必须使用 GET 请求（参考用户提供的万能接口）
            resp = requests.get("https://api.smsbao.com/sms", params=params, timeout=10)
            
            code = resp.text
            msg = error_map.get(code, f"未知错误({code})")
            print(f"📧 短信接口返回: [{code}] {msg}")
            
            if code != '0':
                print(f"⚠️ 短信发送异常: {msg}")
                return False, msg
            return True, "发送成功"
                
        except Exception as e:
            print(f"❌ 短信发送异常: {e}")
            return False, str(e)
        
    def execute_task(self, task):
        log(f"⏰ [自动任务] 开始执行任务: {task['id']}")
        
        # 0. 任务开始前检查 Token
        is_valid, token_msg = client.check_token()
        if not is_valid:
            log(f"❌ Token 失效，任务终止: {token_msg}")
            self.send_notification(f"抢票失败报警：Token已失效({token_msg})，请立即更新！")
            return

        target_date = (datetime.now() + timedelta(days=task['target_day_offset'])).strftime("%Y-%m-%d")
        config = task.get('config')
        
        # 安全检查：确保 config 是字典
        if not isinstance(config, dict):
            # 如果 config 是 None 或其他类型(如int)，重置为空字典以便后续 .get() 调用不报错
            # 但保留 None 的情况供下方旧版兼容逻辑判断
            if config is not None:
                log(f"⚠️ 警告: 任务 {task.get('id')} 的 config 字段类型异常 ({type(config)})，已重置为空字典")
                config = {}

        # 旧版兼容
        if not config and 'items' in task:
            client.submit_order(target_date, task['items'])
            return

        # === 智能抢票核心逻辑 ===
        # 高峰期服务器极不稳定，3次重试远远不够
        # 策略升级：无限重试模式 (Infinite Retry Mode)
        # 直到抢到票、Token失效或人工停止
        retry_interval = CONFIG.get('retry_interval', 0.5)

        # 策略升级：在 12:00 之前的一瞬间（或开始时），如果遇到服务器无响应/404，
        # 我们采用“死磕模式”：高频重试，直到服务器恢复。
        # 用户需求：每 300ms 重试一次。
        aggressive_retry_interval = CONFIG.get('aggressive_retry_interval', 0.3)
        
        attempt = 0
        while True:
            # 重新加载配置，以便在运行时调整速度
            retry_interval = CONFIG.get('retry_interval', 0.5)
            aggressive_retry_interval = CONFIG.get('aggressive_retry_interval', 0.3)

            attempt += 1
            log(f"🔄 第 {attempt} 轮无限尝试...喵")
            
            # 1. 获取最新场地状态
            matrix_res = client.get_matrix(target_date)
            
            # 针对 404 (服务器崩了) 的特殊处理
            if "error" in matrix_res:
                err_msg = matrix_res['error']
                log(f"❌ 获取状态失败: {err_msg} 喵")
                
                # 如果是 404 或 非JSON格式，说明服务器挂了，必须死磕重试
                if "非JSON格式" in err_msg or "404" in err_msg or "无效数据" in err_msg:
                     log(f"⚠️ 检测到服务器 404/崩溃，启用高频重试 ({aggressive_retry_interval}s)...")
                     time.sleep(aggressive_retry_interval)
                     continue
                
                # 如果是会话失效，虽然无限重试也救不回来，但按用户要求“无限”...
                # 不过如果是 Token 失效，继续重试也没意义，还是得退出的
                if "失效" in err_msg or "凭证" in err_msg:
                    log(f"❌ 严重错误: {err_msg}，停止任务")
                    self.send_notification(f"任务停止：Token/Cookie已失效，请更新喵！")
                    return

                # 其他错误重试
                time.sleep(retry_interval)
                continue
                
            matrix = matrix_res['matrix']
            
            target_times = config.get('target_times', [])
            final_items = []
            
            # 模式 A: 优先级序列模式 (Priority)
            if config.get('mode') == 'priority':
                sequences = config.get('priority_sequences', []) # e.g. [["6","7"], ["8","9"]]
                target_count = int(config.get('target_count', 2)) # 总目标数
                allow_partial = config.get('allow_partial', True) # 是否允许拆分 (默认开启以保证数量)

                # === 第一轮：优先尝试完整序列 ===
                for time_slot in target_times:
                    if len(final_items) >= target_count: break
                    
                    for seq in sequences:
                        if len(final_items) >= target_count: break
                        
                        # 如果序列长度 > 剩余需求，且不允许拆分，则跳过
                        # 但如果允许拆分，我们在第二轮处理，所以这里只看能不能完整塞进去
                        if len(seq) > (target_count - len(final_items)):
                            continue

                        # 检查全空闲
                        all_avail = True
                        for p in seq:
                            if p not in matrix or matrix[p].get(time_slot) != "available":
                                all_avail = False
                                break
                        
                        # 检查是否和已选冲突 (虽然第一轮通常不会，但为了健壮性)
                        for p in seq:
                            for item in final_items:
                                if item['place'] == str(p) and item['time'] == time_slot:
                                    all_avail = False; break

                        if all_avail:
                            log(f"   -> 🎯 [优先级-整] 命中完整组合: {seq} @ {time_slot}")
                            for p in seq:
                                final_items.append({"place": str(p), "time": time_slot})

                # === 第二轮：如果没凑够，且允许拆分，则进行散单填充 ===
                if allow_partial and len(final_items) < target_count:
                    log(f"   -> ⚠️ [优先级-散] 完整组合不足，开始散单填充 (目标{target_count}, 已有{len(final_items)})...")
                    for time_slot in target_times:
                        if len(final_items) >= target_count: break
                        
                        for seq in sequences:
                            if len(final_items) >= target_count: break
                            
                            for p in seq:
                                if len(final_items) >= target_count: break
                                
                                # 检查是否可用
                                if p in matrix and matrix[p].get(time_slot) == "available":
                                    # 检查是否已选
                                    is_picked = False
                                    for item in final_items:
                                        if item['place'] == str(p) and item['time'] == time_slot:
                                            is_picked = True; break
                                    
                                    if not is_picked:
                                        log(f"   -> 🧩 [优先级-散] 捡漏: {p}号 @ {time_slot}")
                                        final_items.append({"place": str(p), "time": time_slot})

            # 模式 C: 时间优先模式 (TimePriority)
            elif config.get('mode') == 'time_priority':
                target_count = int(config.get('target_count', 2))
                candidate_places = [str(p) for p in config.get('candidate_places', [])]
                if not candidate_places:
                    candidate_places = [str(i) for i in range(1, 16)] # 假设1-15号场

                sequences = config.get('priority_time_sequences', [])
                # 如果没传序列(旧版前端)，尝试用 target_times 构建单小时序列
                if not sequences and target_times:
                     sequences = [[t] for t in target_times]

                # === 第一轮：优先尝试完整时间序列 ===
                # 目标：找到 target_count 个满足序列的“块”
                # 注意：这里的 target_count 我们理解为“需要的场地数量”
                # 例如 target=2, seq="13-15"(2h). 我们希望找到 2 个场地，每个都能满足 13-15。
                
                # 为了防止重复计数，我们按“轮次”来找
                for i in range(target_count):
                    # 如果已经凑够了 target_count * seq_len (大概估算)，或者无法精确估算
                    # 这里的逻辑是：每一轮尝试满足一个完整的优先序列需求
                    
                    # 遍历每一个优先级序列 (e.g. 13-15, 16-19)
                    found_seq_for_round = False
                    
                    for seq in sequences:
                        if found_seq_for_round: break # 这一轮已经找到一个序列了，跳出，进行下一轮(找第2块)
                        
                        # 在候选场地中找一个能满足 seq 的
                        for p in candidate_places:
                            # 检查该场地是否满足整个 seq
                            all_avail = True
                            for t in seq:
                                # 检查状态
                                if p not in matrix or matrix[p].get(t) != "available":
                                    all_avail = False; break
                                # 检查是否已被之前的轮次选中
                                for item in final_items:
                                    if item['place'] == str(p) and item['time'] == t:
                                        all_avail = False; break
                            
                            if all_avail:
                                # 找到了！拿下！
                                log(f"   -> ⏰ [时间优先-整] 第{i+1}块 命中: {p}号 @ {seq}")
                                for t in seq:
                                    final_items.append({"place": str(p), "time": t})
                                found_seq_for_round = True
                                break # 找到场地了，跳出场地循环
                    
                    if not found_seq_for_round:
                        log(f"   -> ⚠️ [时间优先-整] 第{i+1}块 未能找到完整序列，留给散单填充")

                # === 第二轮：散单填充 ===
                # 如果第一轮没能满足所有需求 (这里的判断标准比较模糊，因为 target_count 是总数)
                # 我们简单点：只要 final_items 里的“总时长”还没达到 target_count * (平均序列长度?) 
                # 不，用户说 target_count 是“总目标数量”。
                # 我们回归最朴素的逻辑：只要还有空位没填满，就拆分序列填。
                # 问题是：target_count 到底是“块数”还是“总预定数”？
                # 假设用户选了 target=2 (意为2个场地)，seq=13-15 (2h)。
                # 理想结果：2个场地 * 2小时 = 4个 bookings。
                # 但 target_count 传过来是 2。
                # 刚才我们修改了前端，允许传 4, 6, 8, 10。
                # 所以我们假设 target_count 是 TOTAL BOOKINGS。
                
                if len(final_items) < target_count:
                    log(f"   -> 🧩 [时间优先-散] 开始散单填充 (当前{len(final_items)}/{target_count})...")
                    # 展平所有序列，按优先级排序
                    flat_priority_times = []
                    for seq in sequences:
                        flat_priority_times.extend(seq)
                    
                    for t in flat_priority_times:
                        if len(final_items) >= target_count: break
                        
                        # 找任意可用场地
                        for p in candidate_places:
                            if len(final_items) >= target_count: break
                            
                            if p in matrix and matrix[p].get(t) == "available":
                                # 查重
                                is_picked = False
                                for item in final_items:
                                    if item['place'] == str(p) and item['time'] == t:
                                        is_picked = True; break
                                
                                if not is_picked:
                                    final_items.append({"place": str(p), "time": t})
                                    log(f"   -> 🧩 [时间优先-散] 捡漏: {p}号 @ {t}")

            # 模式 B: 普通/智能连号模式 (Normal)
            else:
                # 健壮性检查
                if 'candidate_places' not in config:
                    log(f"❌ 任务配置错误: 非优先级模式必须包含 candidate_places")
                    return

                candidate_places = [str(p) for p in config['candidate_places']]
                target_count = int(config.get('target_count', 2))
                smart_mode = config.get('smart_continuous', False)
                
                for time_slot in target_times:
                    if len(final_items) >= target_count:
                        break

                    remaining = target_count - len(final_items)
                    
                    available = []
                    for p in candidate_places:
                        if p in matrix and matrix[p].get(time_slot) == "available":
                            available.append(int(p))
                    available.sort()
                    
                    if not available: continue
                    selected = []
                    
                    # 智能连号：寻找长度为 remaining 或更大的连号
                    # 简化逻辑：优先找最大可能的连号，不超过 remaining
                    if smart_mode:
                        # 尝试找长度为 remaining 的连号，如果不行，找 remaining-1 ...
                        # 这里简单处理：只要有连号优先选
                        for k in range(remaining, 0, -1):
                            if k > len(available): continue
                            for i in range(len(available) - k + 1):
                                window = available[i : i + k]
                                if window[-1] - window[0] == k - 1:
                                    selected = window
                                    break
                            if selected: break
                    
                    if not selected:
                        selected = available[:remaining]
                        
                    if selected:
                        for p in selected:
                            final_items.append({"place": str(p), "time": time_slot})

            
            # 2. 提交结果
            if final_items:
                log(f"🚀 发起抢单: {final_items}")
                res = client.submit_order(target_date, final_items)
                log(f"📊 结果: {res}")
                
                if res['status'] == 'success':
                    log("🎉🎉🎉 抢票成功，任务结束喵！")
                    # 构建详细通知内容
                    try:
                        detail_msg = f"成功抢到{target_date}的场地喵: "
                        items_str = []
                        for item in final_items:
                            items_str.append(f"{item['place']}号场({item['time']})")
                        detail_msg += ",".join(items_str)
                        detail_msg += "OvO喵!"
                        
                        self.send_notification(detail_msg)
                    except Exception as e:
                        log(f"构建短信内容失败: {e}")
                        self.send_notification(f"抢票成功！日期{target_date}，请登录查看喵。")
                        
                    return # 成功退出
                else:
                    log(f"❌ 下单失败: {res.get('msg')}")
                    # 如果是“被抢了”，继续下一轮循环
            else:
                log("⚠️ 本轮未找到任何可用场地")
            
            # 失败后短暂休眠再重试
            # if attempt < max_retries - 1:
            time.sleep(0.5)
                
        # print(" 所有重试均失败，放弃。")

        
    def refresh_schedule(self):
        schedule.clear()
        print(f"🔄 [调度器] 正在刷新任务列表 (共 {len(self.tasks)} 个)...")
        
        for task in self.tasks:
            # 闭包绑定 task
            def job(t=task):
                print(f"⏰ [调度器] 触发任务 ID: {t['id']}")
                self.execute_task(t)
                
            run_time = task['run_time']
            # 确保时间格式是 HH:mm:ss (有的浏览器可能只返回 HH:mm)
            if len(run_time) == 5: run_time += ":00"
            
            try:
                if task['type'] == 'daily':
                    schedule.every().day.at(run_time).do(job)
                    print(f"   -> 已添加每日任务: {run_time}")
                elif task['type'] == 'weekly':
                    days = [schedule.every().monday, schedule.every().tuesday, schedule.every().wednesday,
                            schedule.every().thursday, schedule.every().friday, schedule.every().saturday,
                            schedule.every().sunday]
                    wd = int(task['weekly_day'])
                    days[wd].at(run_time).do(job)
                    print(f"   -> 已添加每周任务: 周{['一','二','三','四','五','六','日'][wd]} {run_time}")
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
    
    # 增加手动抢票成功后的短信通知
    # 只要状态不是 fail，就发送通知（success 或 partial）
    if res.get('status') in ['success', 'partial']:
        print(f"📧 [调试] 准备发送手动抢票通知，状态: {res.get('status')}")
        try:
            status_desc = "手动抢票成功喵！" if res['status'] == 'success' else "手动抢票部分成功喵！"
            detail_msg = f"{status_desc}日期{date}: "
            items_str = []
            for item in items:
                items_str.append(f"{item['place']}号场({item['time']})")
            detail_msg += ",".join(items_str)
            detail_msg += "。请尽快支付喵！"
            
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
            print(f"手动抢票通知发送失败: {e}")
            print(traceback.format_exc())
            
    else:
        print(f"📧 [调试] 抢票状态为 {res.get('status')}，不发送通知。返回msg: {res.get('msg')}")
        
    return jsonify(res)

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(CONFIG)

@app.route('/api/config', methods=['POST'])
def update_config():
    try:
        data = request.json
        
        # 安全保存：先读取现有，再更新
        saved = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
            except: pass

        if 'notification_phones' in data:
            CONFIG['notification_phones'] = data['notification_phones']
            saved['notification_phones'] = CONFIG['notification_phones']
            
        if 'retry_interval' in data:
            try:
                val = float(data['retry_interval'])
                if val < 0.1: val = 0.1 # 最小限制
                CONFIG['retry_interval'] = val
                saved['retry_interval'] = val
            except: pass

        if 'aggressive_retry_interval' in data:
            try:
                val = float(data['aggressive_retry_interval'])
                if val < 0.1: val = 0.1
                CONFIG['aggressive_retry_interval'] = val
                saved['aggressive_retry_interval'] = val
            except: pass
            
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(saved, f, ensure_ascii=False, indent=2)
                
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Update Config Error: {e}")
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

if __name__ == '__main__':
    # 首次启动刷新调度
    task_manager.refresh_schedule()
    print("🚀 服务已启动，访问 http://127.0.0.1:5000")
    print("📋 已加载测试接口: /api/config/test-sms")
    app.run(debug=True, port=5000, use_reloader=False) # 关闭 reloader 防止线程重复启动
