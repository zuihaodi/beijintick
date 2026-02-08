import requests
import json
import urllib.parse
import time
import os

def book_badminton_full_flow(target_date, start_time, end_time, place_num):
    """
    完整模拟羽毛球预定流程：canBook -> getOfferInfo -> getUseCardInfo -> reservationPlace
    """

    # --- 配置区域（统一从 config.json 读取） ---
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    auth = cfg.get('auth', {})

    host = 'gymvip.bfsu.edu.cn'
    token = auth.get('token', '')
    cookie = auth.get('cookie', '')
    card_index = auth.get('card_index', '')
    # Referer 用和 app.py 一样的精简形式即可
    referer = f"https://{host}/easyserp/index.html"

    # 基础 Headers
    headers = {
        "Host": host,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254162e) XWEB/18151 Flue",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": f"https://{host}",
        "Referer": referer,
        "Cookie": cookie
    }

    # 构造基础数据（区分普通场和木地板场）
    try:
        place_int = int(place_num)
    except (TypeError, ValueError):
        place_int = None

    if place_int is not None and place_int >= 15:
        place_short_name = f"mdb{place_num}"
        place_name = f"木地板{place_num}"
    else:
        place_short_name = f"ymq{place_num}"
        place_name = f"羽毛球{place_num}"

    print(f"[*] 开始完整流程: {target_date} {start_time}-{end_time} {place_name}")

    # ==========================================
    # Step 1: canBook (检查是否可预定)
    # ==========================================
    url_canbook = f"https://{host}/easyserpClient/place/canBook"
    
    # 构造 fieldinfo (注意 canBook 的 fieldinfo 比较简单，没有 money)
    canbook_fieldinfo = [
        {
            "day": target_date,
            "startTime": start_time,
            "endTime": end_time,
            "placeShortName": place_short_name
        }
    ]
    # 手动编码
    canbook_fieldinfo_str = urllib.parse.quote(json.dumps(canbook_fieldinfo, separators=(',', ':')))
    
    body_canbook = f"fieldinfo={canbook_fieldinfo_str}&shopNum=1001&token={token}"
    
    print("\n[1/4] 调用 canBook...")
    try:
        resp = requests.post(url_canbook, headers=headers, data=body_canbook, timeout=10)
        print(f"[*] canBook 响应: {resp.text}")
        if '"msg":"success"' not in resp.text:
            print("[-] canBook 失败，终止流程。")
            return
    except Exception as e:
        print(f"[-] canBook 出错: {e}")
        return

    time.sleep(0.5) # 稍微停顿，模拟真实请求间隔

    # ==========================================
    # Step 2: getOfferInfo (获取价格)
    # ==========================================
    url_offer = f"https://{host}/easyserpClient/common/getOfferInfo"
    
    # 构造 projectInfo (和 reservationPlace 的 fieldinfo 结构类似)
    offer_info = [
        {
            "day": target_date,
            "oldMoney": 100,
            "startTime": start_time,
            "endTime": end_time,
            "placeShortName": place_short_name,
            "name": place_name,
            "stageTypeShortName": "ymq"
        }
    ]
    offer_info_str = urllib.parse.quote(json.dumps(offer_info, separators=(',', ':'), ensure_ascii=False))
    
    body_offer = f"token={token}&payMoney=100.00&shopNum=1001&projectType=3&projectInfo={offer_info_str}"
    
    print("\n[2/4] 调用 getOfferInfo...")
    try:
        resp = requests.post(url_offer, headers=headers, data=body_offer, timeout=10)
        # print(f"[*] getOfferInfo 响应: {resp.text[:50]}...") # 不需要打印太多
    except Exception as e:
        print(f"[-] getOfferInfo 出错: {e}")

    time.sleep(0.5)

    # ==========================================
    # Step 3: getUseCardInfo (获取卡片)
    # ==========================================
    url_card = f"https://{host}/easyserpClient/common/getUseCardInfo"
    
    # Body 和 getOfferInfo 几乎一样，只是接口不同
    body_card = f"token={token}&shopNum=1001&projectType=3&projectInfo={offer_info_str}"
    
    print("\n[3/4] 调用 getUseCardInfo...")
    try:
        resp = requests.post(url_card, headers=headers, data=body_card, timeout=10)
        # print(f"[*] getUseCardInfo 响应: {resp.text[:50]}...")
    except Exception as e:
        print(f"[-] getUseCardInfo 出错: {e}")

    time.sleep(0.5)

    # ==========================================
    # Step 4: reservationPlace (核心预定)
    # ==========================================
    url_reserve = f"https://{host}/easyserpClient/place/reservationPlace"
    
    # 构造最复杂的 fieldinfo
    # 注意：必须和抓包完全一致
    reserve_info = [
        {
            "day": target_date,
            "oldMoney": 100,
            "startTime": start_time,
            "endTime": end_time,
            "placeShortName": place_short_name,
            "name": place_name,
            "stageTypeShortName": "ymq",
            "newMoney": 100
        }
    ]
    
    # 手动拼接 JSON 字符串，确保顺序 (虽然 json.dumps 也可以，但为了保险起见使用手动拼接的逻辑)
    # 这里我们使用 json.dumps + ensure_ascii=False + separators，之前验证过这是对的
    reserve_info_str = urllib.parse.quote(json.dumps(reserve_info, separators=(',', ':'), ensure_ascii=False))
    
    # 对中文 type 进行编码
    type_encoded = urllib.parse.quote("羽毛球")
    
    # 构造最终 Body
    # 注意 cardStId=289，这里我们沿用抓包里的值
    body_reserve = (
        f"token={token}&"
        f"shopNum=1001&"
        f"fieldinfo={reserve_info_str}&"
        f"cardStId=289&"
        f"oldTotal=100.00&"
        f"cardPayType=0&"
        f"type={type_encoded}&"
        f"offerId=&"
        f"offerType=&"
        f"total=100.00&"
        f"premerother=&"
        f"cardIndex={card_index}"
    )
    
    print("\n[4/4] 🚀 调用 reservationPlace (下单)...")
    try:
        resp = requests.post(url_reserve, headers=headers, data=body_reserve, timeout=10)
        print(f"[*] 下单响应: {resp.text}")
        
        if '"msg":"success"' in resp.text:
            print("\n[+] 🎉🎉🎉 恭喜！预定成功！")
        elif "数据错误" in resp.text:
            print("[-] 依然报数据错误，可能需要检查 Token 或 cardStId 是否过期。")
        else:
            print("[-] 未知错误，请检查响应。")
            
    except Exception as e:
        print(f"[-] 下单出错: {e}")

if __name__ == "__main__":
    # --- 启动完整流程 ---
    # 注意：这里的日期和场地需要根据实际情况修改
    # 抓包里是 2026-01-10 21:00-22:00 ymq9
    book_badminton_full_flow(
        target_date="2026-01-18",
        start_time="21:00", 
        end_time="22:00", 
        place_num=9
    )
