import requests
import json
import time
import urllib.parse
import sys
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

# ================= 配置区域 =================
CONFIG = {
    "auth": {
        # 抓包获取的 Token 和 Cookie
        "token": "oy9Aj1fKpR3Yxwd6iV7VIlg3Vo-A",
        "cookie": "JSESSIONID=FFE6C0633F33D9CE71354D0D1110AC0D",
        "card_index": "0873612446", # 会员卡号
        "card_st_id": "289",        # 卡策略ID
        "shop_num": "1001"          # 场馆编号
    },
    "strategies": [
        {
            "name": "策略一：周六晚高峰",
            "enable": True,
            "days_offset": 2,       # 0=今天, 1=明天, 2=后天
            "start_time": "21:00",
            "end_time": "22:00",
            "target_count": 2,      # 目标场地数量
            "preferred_courts": [2, 3, 4, 5, 6, 7, 8], # 优先场地列表
            "continuous_priority": True, # 是否优先连号 (如 5号和6号)
            "allow_partial": True   # 如果凑不够连号，是否允许散单
        }
    ],
    "scheduler": {
        "enable": False,            # 是否开启定时抢购
        "target_time": "08:00:00"   # 每天开抢时间
    },
    "notification_phones": ["13910424189"]
}

class AutoBadmintonBooker:
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
        self.session = requests.Session() # 使用 Session 保持长连接

    def log(self, msg):
        """带时间戳的日志"""
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}")

    def notify(self, message):
        """发送通知"""
        phones = CONFIG["notification_phones"]
        self.log(f"🔔 [通知] {message} -> 发送至 {len(phones)} 个号码")
        # TODO: 集成短信 API

    def check_token_validity(self):
        """检查 Token 是否有效 (通过查询余额/卡片信息)"""
        url = f"https://{self.host}/easyserpClient/common/getUseCardInfo"
        # 构造一个虚拟请求体
        dummy_info = urllib.parse.quote(json.dumps([], separators=(',', ':')))
        data = f"token={self.token}&shopNum={CONFIG['auth']['shop_num']}&projectType=3&projectInfo={dummy_info}"
        
        try:
            resp = self.session.post(url, headers=self.headers, data=data, timeout=5)
            if '"msg":"success"' in resp.text:
                self.log("✅ Token 有效，准备就绪")
                return True
            else:
                self.log(f"❌ Token 可能已失效: {resp.text}")
                return False
        except Exception as e:
            self.log(f"⚠️ Token 检查失败 (网络错误): {e}")
            return False # 保守起见，网络错误不阻断流程，但给予警告

    def get_place_matrix(self, date_str):
        """获取场地状态矩阵"""
        url = f"https://{self.host}/easyserpClient/place/getPlaceInfoByShortName"
        params = {
            "shopNum": CONFIG["auth"]["shop_num"],
            "dateymd": date_str,
            "shortName": "ymq",
            "token": self.token
        }
        
        try:
            resp = self.session.get(url, headers=self.headers, params=params, timeout=10)
            data = resp.json()
            
            if data.get("msg") != "success":
                self.log(f"获取场地状态失败: {data.get('msg')}")
                return None
                
            matrix = {}
            # 兼容处理：data['data'] 可能是字符串也可能是对象
            raw_data = data['data']
            if isinstance(raw_data, str):
                raw_list = json.loads(raw_data)
            else:
                raw_list = raw_data
            
            for place in raw_list:
                p_name = place['projectName']['shortname'] # e.g. ymq1
                p_info = place['projectInfo']
                
                time_slots = {}
                for slot in p_info:
                    status_code = slot['state']
                    start = slot['starttime']
                    
                    # 状态码映射
                    if status_code == 4:
                        status = "✅" # 可预定
                    elif status_code == 1:
                        status = "❌" # 已占用
                    else:
                        status = "🚫" # 其他不可用状态
                        
                    time_slots[start] = status
                
                matrix[p_name] = time_slots
                
            return matrix
        except Exception as e:
            self.log(f"解析场地数据出错: {e}")
            return None

    def print_matrix(self, matrix, date_str):
        """打印可视化表格"""
        if not matrix: return
        
        print(f"\n====== {date_str} 场地状态概览 ======")
        times = sorted(list(matrix['ymq1'].keys()))
        
        # 简单的表头对齐
        print(f"{'场地':<6} " + " ".join([f"{t[:2]:<3}" for t in times]))
        print("-" * 60)
        
        sorted_places = sorted(matrix.keys(), key=lambda x: int(x.replace('ymq','')))
        
        for p in sorted_places:
            row = f"{p:<6} "
            for t in times:
                icon = matrix[p].get(t, '  ')
                row += f"{icon:<3} "
            print(row)
        print("="*60 + "\n")

    def book_task(self, date_str, start_time, end_time, place_num):
        """单个下单任务 (用于并发执行)"""
        url = f"https://{self.host}/easyserpClient/place/reservationPlace"
        place_short = f"ymq{place_num}"
        place_name = f"羽毛球{place_num}"
        
        info = [{
            "day": date_str, "oldMoney": 100, "startTime": start_time, "endTime": end_time,
            "placeShortName": place_short, "name": place_name, "stageTypeShortName": "ymq", "newMoney": 100
        }]
        info_str = urllib.parse.quote(json.dumps(info, separators=(',', ':'), ensure_ascii=False))
        type_encoded = urllib.parse.quote("羽毛球")
        
        body = (
            f"token={self.token}&shopNum={CONFIG['auth']['shop_num']}&fieldinfo={info_str}&"
            f"cardStId={CONFIG['auth']['card_st_id']}&oldTotal=100.00&cardPayType=0&"
            f"type={type_encoded}&offerId=&offerType=&total=100.00&premerother=&"
            f"cardIndex={CONFIG['auth']['card_index']}"
        )
        
        try:
            self.log(f"🚀 发起抢单: {place_name} ...")
            resp = self.session.post(url, headers=self.headers, data=body, timeout=5)
            
            if '"msg":"success"' in resp.text:
                self.log(f"🎉🎉🎉 成功锁定: {place_name}")
                return place_num
            else:
                self.log(f"❌ 失败 ({place_name}): {resp.json().get('data', resp.text)}")
                return None
        except Exception as e:
            self.log(f"⚠️ 异常 ({place_name}): {e}")
            return None

    def find_continuous_courts(self, available_courts, target_count):
        """寻找最佳连号组合"""
        if len(available_courts) < target_count:
            return []
            
        # 排序
        sorted_courts = sorted(available_courts)
        
        # 寻找连续序列
        # 例如: [1, 2, 3, 5, 6] target=2 -> [[1,2], [2,3], [5,6]]
        best_combo = []
        
        for i in range(len(sorted_courts) - target_count + 1):
            window = sorted_courts[i : i + target_count]
            # 检查窗口内的数字是否连续
            if window[-1] - window[0] == target_count - 1:
                return window # 找到第一组连号就返回 (优先前面的场地)
                
        return []

    def execute_strategies(self):
        """执行策略主逻辑"""
        
        # 1. 预检 Token
        if not self.check_token_validity():
            self.log("警告: Token 可能无效，但脚本将继续尝试...")

        for strategy in CONFIG["strategies"]:
            if not strategy["enable"]: continue
            
            target_date = (datetime.now() + timedelta(days=strategy["days_offset"])).strftime("%Y-%m-%d")
            self.log(f"执行策略: {strategy['name']} [日期: {target_date}, 时间: {strategy['start_time']}]")
            
            # 2. 获取状态并筛选
            matrix = self.get_place_matrix(target_date)
            if matrix:
                self.print_matrix(matrix, target_date)
            
            # 筛选符合时间段的空闲场地
            available_courts = []
            for num in strategy["preferred_courts"]:
                p_short = f"ymq{num}"
                # 严格检查状态
                if matrix and matrix.get(p_short, {}).get(strategy["start_time"]) == "✅":
                    available_courts.append(num)
            
            self.log(f"可用场地列表: {available_courts}")
            
            if not available_courts:
                self.log("没有符合条件的空闲场地，跳过此策略。")
                continue

            # 3. 确定抢购目标
            targets = []
            
            # 优先连号逻辑
            if strategy["continuous_priority"]:
                targets = self.find_continuous_courts(available_courts, strategy["target_count"])
                if targets:
                    self.log(f"找到完美连号组合: {targets}")
            
            # 如果没找到连号，或者允许散单
            if not targets and strategy["allow_partial"]:
                targets = available_courts[:strategy["target_count"]]
                self.log(f"使用散单组合: {targets}")
            
            if not targets:
                self.log("无法凑齐目标数量，且策略不允许部分抢购。")
                continue

            # 4. 并发抢单 (核心优化)
            self.log(f"启动并发抢单，目标: {targets}")
            success_list = []
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = []
                for num in targets:
                    futures.append(executor.submit(
                        self.book_task, 
                        target_date, 
                        strategy["start_time"], 
                        strategy["end_time"], 
                        num
                    ))
                
                # 收集结果
                for f in futures:
                    res = f.result()
                    if res: success_list.append(res)
            
            # 5. 结果汇总
            if success_list:
                msg = f"成功抢到 {len(success_list)} 块场地: {success_list} (日期: {target_date})"
                self.log(msg)
                self.notify(msg)
            else:
                self.log("本轮抢购全部失败。")

    def run(self):
        """运行入口 (含定时逻辑)"""
        if CONFIG["scheduler"]["enable"]:
            target_time = CONFIG["scheduler"]["target_time"]
            self.log(f"定时模式已开启，等待 {target_time} ...")
            
            while True:
                now = datetime.now().strftime("%H:%M:%S")
                if now == target_time:
                    self.log("⏰ 时间到！开始行动！")
                    self.execute_strategies()
                    break # 执行一次后退出，或者改为 sleep 60 继续等待明天
                time.sleep(0.5)
        else:
            self.execute_strategies()

if __name__ == "__main__":
    booker = AutoBadmintonBooker()
    booker.run()
