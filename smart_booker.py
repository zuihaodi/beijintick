import requests
import json
import time
import urllib.parse
import schedule
import threading
from datetime import datetime, timedelta

# ================= 配置区域 =================
CONFIG = {
    "auth": {
        # 这些是从抓包中获取的凭证。
        # 关于自动登录：由于是微信环境，建议在电脑浏览器(伪装UA)打开链接扫码，
        # 然后获取 Cookie 和 Token 填入此处。只要不退出登录，通常有效期可达数天。
        "token": "oy9Aj1fKpR3Yxwd6iV7VIlg3Vo-A",
        "cookie": "JSESSIONID=FFE6C0633F33D9CE71354D0D1110AC0D",
        "card_index": "0873612446",
        "card_st_id": "289", # 之前推测的ID，也可能是5759，需根据实际情况填
        "shop_num": "1001"
    },
    "notification": {
        "enable": True,
        # 实际发送短信需要对接阿里云/腾讯云SMS API，或者使用简单的 Server酱/PushPlus
        "phones": ["13800138000", "13900139000"] 
    },
    "strategies": [
        {
            "name": "周六晚高峰策略",
            "enable": True,
            # 预订日期模式: "offset" (相对天数) 或 "fixed" (固定日期)
            "date_mode": "offset", 
            "date_value": 2, # 2表示预订后天的场地
            "time_start": "21:00",
            "time_end": "22:00",
            "target_count": 2, # 目标预订数量
            # 场地优先列表
            "preferred_courts": [2, 3, 4, 5, 6, 7, 8], 
            "prefer_continuous": True, # 连续优先 (简单的逻辑：优先尝试相邻的号)
            "allow_partial": True, # 允许部分预订 (不够2块时，能抢几块是几块)
        }
    ],
    "scheduler": {
        "enable": False, # 是否开启定时任务 (测试时建议 False)
        "run_time": "12:00" # 每天开抢时间
    }
}

# ================= 核心逻辑类 =================

class BadmintonBooker:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.host = "gymvip.bfsu.edu.cn"
        self.headers = {
            "Host": self.host,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254162e) XWEB/18151 Flue",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"https://{self.host}",
            "Referer": f"https://{self.host}/easyserp/index.html",
            "Cookie": self.config["auth"]["cookie"]
        }
        self.token = self.config["auth"]["token"]

    def log(self, msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def send_notification(self, message):
        """发送通知 (模拟)"""
        if not self.config["notification"]["enable"]:
            return
        
        phones = self.config["notification"]["phones"]
        self.log(f"正在向 {len(phones)} 个手机号发送通知: {message}")
        # 在这里对接真实的 SMS API
        # 例如: requests.post("https://sms-api.com/send", json={...})
        for phone in phones:
            print(f"  -> [SMS] To {phone}: {message}")

    def check_token(self):
        """验证 Token 是否有效"""
        url = f"https://{self.host}/easyserpClient/common/getOfferInfo"
        # 构造一个虚拟的查询包
        dummy_info = [{"day": datetime.now().strftime("%Y-%m-%d"), "startTime": "09:00", "endTime": "10:00", "placeShortName": "ymq1"}]
        data = {
            "token": self.token,
            "payMoney": "0.00",
            "shopNum": self.config["auth"]["shop_num"],
            "projectType": "3",
            "projectInfo": urllib.parse.quote(json.dumps(dummy_info, separators=(',', ':'), ensure_ascii=False))
        }
        try:
            resp = self.session.post(url, headers=self.headers, data=data, timeout=5)
            if '"msg":"success"' in resp.text:
                self.log("登录状态校验通过 ✅")
                return True
            else:
                self.log(f"登录状态失效 ❌: {resp.text}")
                return False
        except Exception as e:
            self.log(f"网络错误: {e}")
            return False

    def check_availability(self, date_str, start_time, end_time, court_num):
        """
        检查单个场地是否可预定 (调用 canBook)
        虽然 canBook 是 check 接口，但频繁调用可能会被封，
        但在没有 getPlaceState 接口的情况下，这是唯一的方法。
        """
        url = f"https://{self.host}/easyserpClient/place/canBook"
        place_short = f"ymq{court_num}"
        
        info = [{
            "day": date_str,
            "startTime": start_time,
            "endTime": end_time,
            "placeShortName": place_short
        }]
        
        info_str = urllib.parse.quote(json.dumps(info, separators=(',', ':')))
        data = f"fieldinfo={info_str}&shopNum={self.config['auth']['shop_num']}&token={self.token}"
        
        try:
            resp = self.session.post(url, headers=self.headers, data=data, timeout=3)
            # 如果返回 success，说明可定
            if '"msg":"success"' in resp.text:
                return True
            return False
        except:
            return False

    def book_court(self, date_str, start_time, end_time, court_num):
        """执行下单"""
        url = f"https://{self.host}/easyserpClient/place/reservationPlace"
        
        place_short = f"ymq{court_num}"
        place_name = f"羽毛球{court_num}"
        
        info = [{
            "day": date_str,
            "oldMoney": 100, # 这里假设价格固定100，实际可能需要从 query 结果拿
            "startTime": start_time,
            "endTime": end_time,
            "placeShortName": place_short,
            "name": place_name,
            "stageTypeShortName": "ymq",
            "newMoney": 100
        }]
        
        info_str = urllib.parse.quote(json.dumps(info, separators=(',', ':'), ensure_ascii=False))
        type_encoded = urllib.parse.quote("羽毛球")
        
        # 组装 Body
        body = (
            f"token={self.token}&"
            f"shopNum={self.config['auth']['shop_num']}&"
            f"fieldinfo={info_str}&"
            f"cardStId={self.config['auth']['card_st_id']}&"
            f"oldTotal=100.00&"
            f"cardPayType=0&"
            f"type={type_encoded}&"
            f"offerId=&"
            f"offerType=&"
            f"total=100.00&"
            f"premerother=&"
            f"cardIndex={self.config['auth']['card_index']}"
        )
        
        try:
            self.log(f"正在抢订 -> {date_str} {place_name} ...")
            resp = self.session.post(url, headers=self.headers, data=body, timeout=5)
            self.log(f"结果: {resp.text}")
            
            if '"msg":"success"' in resp.text:
                return True, "预定成功"
            elif "数据错误" in resp.text:
                return False, "数据错误(可能ID不对)"
            elif "操作过快" in resp.text:
                return False, "操作过快"
            else:
                return False, "未知错误"
        except Exception as e:
            return False, str(e)

    def execute_strategy(self, strategy):
        """执行单条策略"""
        if not strategy["enable"]:
            return

        self.log(f"=== 开始执行策略: {strategy['name']} ===")
        
        # 1. 计算日期
        if strategy["date_mode"] == "offset":
            target_date = (datetime.now() + timedelta(days=strategy["date_value"])).strftime("%Y-%m-%d")
        else:
            target_date = strategy["date_value"]
            
        start_time = strategy["time_start"]
        end_time = strategy["time_end"]
        
        self.log(f"目标: {target_date} {start_time}-{end_time}, 目标数量: {strategy['target_count']}")
        
        # 2. 生成场地尝试顺序
        # 简单的连续优先逻辑：如果 prefer_continuous 为 True，我们不做特殊排序，
        # 因为输入的 preferred_courts 已经是 [2,3,4...] 这种顺序了。
        # 真正的连续检测需要先查询所有状态再计算，耗时太久。
        # 抢票核心是：快。直接按列表顺序尝试即可。
        
        courts_to_try = strategy["preferred_courts"]
        success_count = 0
        success_courts = []
        
        # 3. 循环尝试
        for court_num in courts_to_try:
            if success_count >= strategy["target_count"]:
                break
                
            # 策略：直接抢，不查！(查了再抢通常来不及)
            # 或者：如果允许“一块不订”，才需要先查再原子操作(但该系统不支持批量原子下单)
            
            # 尝试下单
            success, msg = self.book_court(target_date, start_time, end_time, court_num)
            
            if success:
                success_count += 1
                success_courts.append(f"羽毛球{court_num}")
                self.log(f"🎉 成功锁定: 羽毛球{court_num}")
            else:
                # 失败处理：如果是因为操作过快，稍微等一下？
                if "操作过快" in msg:
                    time.sleep(1)
            
            # 延时策略：每单间隔，避免封号
            time.sleep(0.5)
            
        # 4. 结果结算
        if success_count > 0:
            final_msg = f"抢票成功！日期:{target_date}, 时间:{start_time}, 场地:{','.join(success_courts)}"
            self.send_notification(final_msg)
            
            # 检查数量是否足够
            if success_count < strategy["target_count"]:
                if not strategy["allow_partial"]:
                    # 这是一个悲剧：订到了但不满足数量。
                    # 通常系统不支持自动退订，所以只能发通知人工处理。
                    self.send_notification(f"⚠️ 警告：仅订到 {success_count} 块，未达到目标 {strategy['target_count']} 块，请及时处理！")
        else:
            self.log("本轮策略结束，未成功预定任何场地。")

# ================= 调度器 =================

def job():
    print("\n⏰ 定时任务触发！")
    booker = BadmintonBooker(CONFIG)
    
    # 先检查 Token，如果失效发报警
    if not booker.check_token():
        booker.send_notification("🚨 抢票脚本报警：登录凭证(Token)已失效，请立即更新！")
        return

    # 执行所有策略
    for strategy in CONFIG["strategies"]:
        booker.execute_strategy(strategy)

def run_scheduler():
    run_time = CONFIG["scheduler"]["run_time"]
    print(f"[*] 调度器已启动，将在每天 {run_time} 执行任务...")
    
    schedule.every().day.at(run_time).do(job)
    
    while True:
        schedule.run_pending()
        time.sleep(1)

# ================= 主程序 =================

if __name__ == "__main__":
    # 模式选择
    print("1. 立即执行策略 (测试用)")
    print("2. 启动定时任务 (挂机用)")
    # choice = input("请选择模式 (1/2): ")
    choice = "1" # 默认立即执行，方便你测试
    
    if choice == "1":
        job()
    else:
        run_scheduler()
