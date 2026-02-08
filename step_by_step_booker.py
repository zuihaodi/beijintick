import requests
import json
import time
import urllib.parse
import sys
from datetime import datetime, timedelta

# ================= 配置区域 =================
CONFIG = {
    "auth": {
        # 请确保这里的 Token 是有效的，如果失效请替换
        "token": "oy9Aj1fKpR3Yxwd6iV7VIlg3Vo-A",
        "cookie": "JSESSIONID=FFE6C0633F33D9CE71354D0D1110AC0D",
        "card_index": "0873612446",
        "card_st_id": "289", 
        "shop_num": "1001"
    }
}

class StepByStepBooker:
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

    def select_date(self):
        """第一步：选择日期"""
        print("\n=== 第一步：选择预定日期 ===")
        today = datetime.now()
        options = []
        
        # 列出未来7天
        for i in range(7):
            d = today + timedelta(days=i)
            d_str = d.strftime("%Y-%m-%d")
            week_day = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d.weekday()]
            options.append(d_str)
            print(f"{i+1}. {d_str} ({week_day}) {'[今天]' if i==0 else ''}")
            
        while True:
            choice = input("\n请选择序号 (1-7): ").strip()
            if choice.isdigit() and 1 <= int(choice) <= 7:
                selected_date = options[int(choice)-1]
                print(f"-> 您选择了: {selected_date}")
                return selected_date
            print("输入无效，请重新输入。")

    def fetch_and_show_matrix(self, date_str):
        """第二步：爬取并展示场地信息"""
        print(f"\n=== 第二步：正在爬取 {date_str} 的场地信息... ===")
        
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
                print(f"❌ 获取失败: {data.get('msg')}")
                print("提示：可能是 Token 过期了。")
                return None
                
            # 解析数据
            raw_data = data.get('data')
            if not raw_data:
                print("❌ 服务器返回的数据为空 (data 字段不存在)")
                return None
                
            # 兼容处理：data 可能是字符串，也可能是列表/对象
            if isinstance(raw_data, str):
                try:
                    raw_list = json.loads(raw_data)
                except json.JSONDecodeError:
                    print(f"❌ 数据解析失败，原始数据不是合法的 JSON 字符串: {raw_data[:50]}...")
                    return None
            else:
                raw_list = raw_data
            
            # 再次检查 raw_list 是否为列表
            if isinstance(raw_list, dict):
                 # 如果是字典，可能是 {'times': [...], 'placeArray': [...]} 这种结构
                 print(f"[*] 检测到数据是字典结构，正在寻找场地列表...")
                 print(f"[*] 包含的字段: {list(raw_list.keys())}")
                 
                 # 尝试寻找包含 'place' 或 'Array' 的字段
                 possible_keys = ['placeArray', 'data', 'list', 'places']
                 found_list = None
                 for key in possible_keys:
                     if key in raw_list and isinstance(raw_list[key], list):
                         found_list = raw_list[key]
                         print(f"[*] 成功在字段 '{key}' 中找到列表")
                         break
                 
                 if found_list:
                     raw_list = found_list
                 else:
                     print("❌ 无法在字典中找到场地列表字段。")
                     return None

            if not isinstance(raw_list, list):
                 print(f"❌ 数据格式异常，期望是列表，实际是: {type(raw_list)}")
                 # 尝试打印一点内容看看
                 print(f"内容预览: {str(raw_list)[:100]}")
                 return None

            matrix = {} # { "ymq1": {"10:00": "可用", ...} }
            
            for place in raw_list:
                # 健壮性检查：确保字段存在
                if 'projectName' not in place or 'shortname' not in place['projectName']:
                    continue
                    
                p_name = place['projectName']['shortname'] # ymq1
                p_info = place.get('projectInfo', [])
                
                time_slots = {}
                for slot in p_info:
                    status_code = slot['state']
                    start = slot['starttime']
                    
                    # 修正后的状态码映射：
                    # 根据用户反馈，14:00(state:1)是绿色的/可选的
                    # 所以 state:1 = 可预定，state:4 = 不可预定
                    if status_code == 1:
                        status = "✅"
                    else:
                        status = "⛔" 
                        
                    time_slots[start] = status
                
                matrix[p_name] = time_slots
            
            # === 可视化展示 (转置版：X=场地, Y=时间) ===
            if not matrix:
                print("未获取到场地数据。")
                return None
                
            print(f"\n场地状态表 (✅=可预定, ⛔=已占用/不可用)")
            
            # 获取所有时间点并排序
            times = sorted(list(matrix['ymq1'].keys()))
            
            # 辅助排序函数 (保持之前的逻辑)
            def sort_key(x):
                import re
                match = re.search(r'(\d+)$', x)
                if match: return int(match.group(1))
                return 999

            sorted_places = sorted(matrix.keys(), key=sort_key)
            
            # 1. 打印表头 (场地号)
            # 动态计算每个列宽，假设每个格子占5个字符
            col_width = 5
            header = "时间   " 
            for p in sorted_places:
                # 简化场地名: ymq1 -> 1, mdb15 -> 15
                short_p = p.replace('ymq','').replace('mdb','M')
                header += f"{short_p:<{col_width}}"
            
            print("-" * len(header))
            print(header)
            print("-" * len(header))
            
            # 2. 打印每一行 (时间)
            for t in times:
                row = f"{t:<7}" # 时间列
                for p in sorted_places:
                    icon = matrix[p].get(t, '  ')
                    # 对齐处理：✅ 占2字符但显示宽度不一，补空格
                    # 这里简单的处理，✅后补3空，空白补5空
                    if icon == '✅':
                        cell = "✅   "
                    else:
                        cell = "     " # 5个空格
                    
                    # 尝试自适应对齐 (如果是控制台等宽字体)
                    row += f"{icon:<{col_width}}" 
                print(row)
                
            print("-" * len(header))
            
            return matrix, sorted_places, times
            
        except Exception as e:
            print(f"❌ 发生错误: {e}")
            return None

    def select_court_and_time(self, matrix, sorted_places, times):
        """第三步：优化版选择流程 (支持多选)"""
        print("\n=== 第三步：筛选场地 (支持多选) ===")
        
        selected_items = [] # 存储多组 (place_num, start_time, end_time)
        
        while True:
            # 1. 预处理可用场地
            available_places = []
            for p in sorted_places:
                slots = [t for t, status in matrix[p].items() if status == "✅"]
                if slots:
                    available_places.append((p, slots))
            
            if not available_places:
                print("❌ 没有更多可预定的场地了！")
                break
                
            # 2. 列出场地
            print("\n[当前可用场地列表]")
            for idx, (p_name, slots) in enumerate(available_places):
                if p_name.startswith('ymq'): display = f"羽毛球{p_name.replace('ymq','')}"
                else: display = p_name
                print(f"{idx+1}. {display} (剩余 {len(slots)} 个时段)")
                
            # 3. 选择场地
            print("\n请输入场地序号 (输入 0 结束选择并去下单):")
            choice = input(">>> ").strip()
            
            if choice == '0':
                if not selected_items:
                    print("⚠️ 您还没选任何场地呢！")
                    continue
                break
                
            if not (choice.isdigit() and 1 <= int(choice) <= len(available_places)):
                print("输入无效。")
                continue
                
            selected_place, available_slots = available_places[int(choice)-1]
            p_num = selected_place.replace('ymq','').replace('mdb','')
            
            # 4. 选择时间
            print(f"\n--- 选择 {selected_place} 的时间 ---")
            available_slots.sort()
            for idx, t in enumerate(available_slots):
                print(f"{idx+1}. {t} - {t[:2]}:59")
                
            t_choice = input("请输入时间序号: ").strip()
            if not (t_choice.isdigit() and 1 <= int(t_choice) <= len(available_slots)):
                print("输入无效。")
                continue
                
            selected_time = available_slots[int(t_choice)-1]
            
            # 计算结束时间
            try:
                st_obj = datetime.strptime(selected_time, "%H:%M")
                et_obj = st_obj + timedelta(hours=1)
                end_time = et_obj.strftime("%H:%M")
            except:
                end_time = "22:00"
            
            # 添加到购物车
            item = (p_num, selected_time, end_time)
            selected_items.append(item)
            print(f"✅ 已添加: 羽毛球{p_num} {selected_time}-{end_time}")
            
            # 询问是否继续
            print(f"当前已选 {len(selected_items)} 个场地。")
            confirm = input("是否继续添加其他场地？(y/n) [y]: ").strip().lower()
            if confirm == 'n':
                break

        return selected_items

    def submit_order(self, date_str, selected_items):
        """第四步：提交合并订单"""
        if not selected_items: return

        print(f"\n=== 第四步：正在提交合并订单 ({len(selected_items)} 个场地)... ===")
        
        # 构造 fieldinfo 数组
        field_info_list = []
        total_money = 0

        for p_num, start, end in selected_items:
            # 根据场地号区分普通场 (1-14) 和木地板场 (15-17)
            try:
                p_int = int(p_num)
            except (TypeError, ValueError):
                p_int = None

            if p_int is not None and p_int >= 15:
                place_short = f"mdb{p_num}"
                place_name = f"木地板{p_num}"
            else:
                place_short = f"ymq{p_num}"
                place_name = f"羽毛球{p_num}"

            info = {
                "day": date_str,
                "oldMoney": 100,  # 假设单价100，实际应从 getOfferInfo 获取
                "startTime": start,
                "endTime": end,
                "placeShortName": place_short,
                "name": place_name,
                "stageTypeShortName": "ymq",
                "newMoney": 100
            }
            field_info_list.append(info)
            total_money += 100

        # 序列化
        info_str = urllib.parse.quote(json.dumps(field_info_list, separators=(',', ':'), ensure_ascii=False))
        type_encoded = urllib.parse.quote("羽毛球")
        
        body = (
            f"token={self.token}&"
            f"shopNum={CONFIG['auth']['shop_num']}&"
            f"fieldinfo={info_str}&"
            f"cardStId={CONFIG['auth']['card_st_id']}&"
            f"oldTotal={total_money}.00&" # 动态计算总价
            f"cardPayType=0&"
            f"type={type_encoded}&"
            f"offerId=&"
            f"offerType=&"
            f"total={total_money}.00&" # 动态计算总价
            f"premerother=&"
            f"cardIndex={CONFIG['auth']['card_index']}"
        )
        
        try:
            url = f"https://{self.host}/easyserpClient/place/reservationPlace"
            resp = self.session.post(url, headers=self.headers, data=body, timeout=10)
            print(f"[*] 服务器响应: {resp.text}")
            
            if '"msg":"success"' in resp.text:
                print("\n🎉🎉🎉 合并下单成功！请尽快去支付！")
            elif "数据错误" in resp.text:
                print("\n❌ 下单失败: 数据错误")
            else:
                print(f"\n❌ 下单失败: {resp.json().get('data', '未知错误')}")
                
        except Exception as e:
            print(f"[-] 网络错误: {e}")

def main():
    booker = StepByStepBooker()
    
    # 1. 选日期
    date_str = booker.select_date()
    
    # 2. 爬取并显示
    result = booker.fetch_and_show_matrix(date_str)
    if not result: return
    matrix, sorted_places, times = result
    
    # 3. 多选场地
    selected_items = booker.select_court_and_time(matrix, sorted_places, times)
    if not selected_items:
        print("已取消操作。")
        return
        
    # 4. 提交合并订单
    booker.submit_order(date_str, selected_items)


if __name__ == "__main__":
    main()
