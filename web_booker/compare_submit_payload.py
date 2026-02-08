import json
import requests

import app
import app_before_auto


# --- 这里我们拦截所有 Session.post，防止真实网络请求 ---
class DummyResp:
    def __init__(self):
        self.status_code = 200
        self.text = '{"msg": "success", "data": null}'

    def json(self):
        return json.loads(self.text)


def make_fake_post(tag, payload_store):
    """
    tag：标记是哪个版本（app / before）
    payload_store：把 data 存进字典，方便后面对比
    """
    def fake_post(self, url, data=None, headers=None, timeout=None, **kwargs):
        print(f"\n===== {tag} 调用 requests.Session.post =====")
        print("URL:", url)
        print("HEADERS 部分：", {k: headers.get(k) for k in ["Host", "Referer"] if headers and k in headers})
        print("RAW DATA:", data)

        # 把 JSON 字符串转成对象存起来，方便后面对比
        try:
            payload_store[tag] = json.loads(data)
        except Exception:
            payload_store[tag] = data

        # 返回一个假的响应，避免代码里继续访问 resp.json() 报错
        return DummyResp()

    return fake_post


def run_one(module, tag, payload_store):
    # 重新打补丁，确保每次 run_one 都用自己的 fake_post
    requests.Session.post = make_fake_post(tag, payload_store)

    # 用模块里的 ApiClient（两个文件里类名是一样的）
    client = module.ApiClient()

    # 构造一组“选中的场次”，写你习惯的几条就行，重点是两个版本用同一份
    selected_items = [
        {"place": 9, "time": "14:00"},
        {"place": 9, "time": "15:00"},
        {"place": 10, "time": "14:00"},
        {"place": 10, "time": "15:00"},
    ]

    # 随便给一个日期，格式和你平时一致即可
    client.submit_order("2026-01-22", selected_items)


if __name__ == "__main__":
    payloads = {}

    # 先跑旧版本
    run_one(app_before_auto, "before_auto", payloads)

    # 再跑新版本
    run_one(app, "app_new", payloads)

    print("\n========== 对比结果 ==========")
    before = payloads.get("before_auto")
    new = payloads.get("app_new")

    print("旧版本 payload: ", before)
    print("新版本 payload: ", new)

    # 如果是 JSON 对象，这里可以直接判断是否完全一样
    try:
        print("两份 payload 是否完全一致:", before == new)
    except Exception as e:
        print("无法直接比较：", e)
