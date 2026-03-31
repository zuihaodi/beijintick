# 羽毛球场地抢票助手

核心目标：在高峰期抢指定日期、指定时间段的场地。尽量按「同一时段连号场地」来选格；若矩阵里凑不出连号，应退而求其次用「同一时段多块散场」把缺口补上，绝对禁止有场地但不提交请求。每次拉矩阵，只要包含符合需求的，必须至少拿1块。

## 注意事项

用户需求目标：满足「指定块数 + 指定时段 + 偏好区间」即可，不要求必须固定场号，优先连续号码。

代码不要搞的复杂，要简单清晰，直接实现功能。避免弄很多分支，弄很多开关。
---

## 订场核心规则与经验教训（避免反复犯同样错误）

以下为根据多次 12 点抢场实战复盘归纳的**规则、经验与教训**，供配置与排错时对照。

1. **「too fast操作过快」的本质**  
   直接进行提交订单 submit 就可能被直接判为「操作过快」并拒绝。必须在提交订单前几秒钟有一个查询矩阵get_matrix的请求，一次查询矩阵后，可以分多批提交，批次之间的时间间隔应为5s左右，不得过短，否则会引发“数据错误，请重试”。过长也不可以，但目前未测最大时间是多少。

2. **批次post格式要求**  
   同一批的记录数量必须小于等于账号最大可预订场地数（一般是1块、2块、3块）
   单条记录支持1个场地+连续时间段的格式，比如 10号 开始时间20点 结束时间22点。
   假如当前账号支持2块场地，则提交的数据如下，一批即可。如果把场地拆成独立小块为一条记录，则会有4条记录，只能2块一批，发两批。
   fieldinfo（URL 解码后的 JSON 字面量）:
[
  {
    "day": "2026-04-05",
    "oldMoney": 200,
    "startTime": "18:00",
    "endTime": "20:00",
    "placeShortName": "mdb15",
    "name": "木地板15",
    "stageTypeShortName": "ymq",
    "newMoney": 200
  },
  {
    "day": "2026-04-05",
    "oldMoney": 200,
    "startTime": "18:00",
    "endTime": "20:00",
    "placeShortName": "mdb16",
    "name": "木地板16",
    "stageTypeShortName": "ymq",
    "newMoney": 200
  }
]

3. **首矩阵要尽早拿到，拿到后立即发订单**  
   黄金窗口只有几十秒，拿到矩阵是最重要的，期间网络的延时比较严重，程序应该设计足够的超时时间，避免自己过早放弃，拿到矩阵后立即发订单。

4. **“接口已 success”基本上99%成功**  
   只有接口明确返回错误意义：操作过快是缺少前置的get_matrix，数据错误是选型错误，日期错误大多是已经被预订了。永远不要用mine覆盖（include_mine_overlay）判断是否被我预订。

矩阵状态：1-available；2-mine；4-booked；6-locked

## 发布前自检（推荐）

建议每次改动 `web_booker/templates/index.html` 后先执行：

```bash
python - <<'PY'
from jinja2 import Environment
from pathlib import Path
Environment().parse(Path('web_booker/templates/index.html').read_text(encoding='utf-8'))
print('template syntax ok')
PY
```



## 抢订成功率提升计划（窗口 30-60 秒）


接口网络数据参考：
【获取卡信息by场地id】
GET /easyserpClient/place/getInfStCardByStId?id=2&shopNum=1001&token=oy9Aj1eCxLy5xnWwRmc5eK_7GDRU HTTP/2
host: gymvip.bfsu.edu.cn
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254173b) XWEB/19027 Flue
accept: application/json, text/plain, */*
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=051Zp2ll24Wlqh4uWBnl2pDzcc4Zp2l3&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
priority: u=1, i



HTTP/2 200
server: nginx
date: Thu, 26 Mar 2026 04:10:52 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
content-encoding: gzip

{"msg":"success","data":{"id":291,"cardShortName":"xwtk5q","lineNumber":1,"infstId":2,"cardName":"校外通卡5q"}}


【拉矩阵-已解锁】
GET /easyserpClient/place/getPlaceInfoByShortName?shopNum=1001&dateymd=2026-03-22&shortName=ymq&token=oy9Aj1Y7gmOS31lnOQgkXiEvgoyc HTTP/2
host: gymvip.bfsu.edu.cn
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254173b) XWEB/19027 Flue
accept: application/json, text/plain, */*
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=051uPrFa1yf9pL0DypHa1mUq5C1uPrFc&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
priority: u=1, i

返回数据

HTTP/2 200
server: nginx
date: Sun, 22 Mar 2026 14:43:55 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
content-encoding: gzip

{"msg":"success","data":{"times":["10:00","11:00","12:00","13:00","14:00","15:00","16:00","17:00","18:00","19:00","20:00","21:00","22:00"],"placeArray":[{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":3,"aAtype":0,"curUserCount":1,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq1","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":1,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球1","stagetypeshortname":"ymq","maxUserCount":1,"id":68,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":201,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq2","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":2,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球2","stagetypeshortname":"ymq","maxUserCount":1,"id":69,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":2},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":2},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":399,"aAtype":0,"curUserCount":1,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq3","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":3,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球3","stagetypeshortname":"ymq","maxUserCount":1,"id":70,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":597,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq4","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":4,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球4","stagetypeshortname":"ymq","maxUserCount":1,"id":71,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":795,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq5","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":5,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球5","stagetypeshortname":"ymq","maxUserCount":1,"id":72,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":993,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq6","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":6,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球6","stagetypeshortname":"ymq","maxUserCount":1,"id":73,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":1191,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq7","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":7,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球7","stagetypeshortname":"ymq","maxUserCount":1,"id":74,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":1192,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq8","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":8,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球8","stagetypeshortname":"ymq","maxUserCount":1,"id":75,"state":"下线","vValue":125}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":992,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq9","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":9,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球9","stagetypeshortname":"ymq","maxUserCount":1,"id":76,"state":"下线","vValue":129}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":798,"aAtype":0,"curUserCount":1,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq10","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":10,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球10","stagetypeshortname":"ymq","maxUserCount":1,"id":77,"state":"下线","vValue":127}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":597,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq11","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":11,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球11","stagetypeshortname":"ymq","maxUserCount":1,"id":78,"state":"下线","vValue":131}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":402,"aAtype":0,"curUserCount":1,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq12","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":12,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球12","stagetypeshortname":"ymq","maxUserCount":1,"id":79,"state":"下线","vValue":133}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":198,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq13","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":13,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球13","stagetypeshortname":"ymq","maxUserCount":1,"id":80,"state":"下线","vValue":136}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":2},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":2},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":4,"aAtype":0,"curUserCount":1,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq14","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":14,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球14","stagetypeshortname":"ymq","maxUserCount":1,"id":81,"state":"下线","vValue":136}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":2,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"mdb15","tipCount":0,"isHorizontal":1,"masterId":"","stagetype":"羽毛球","hardwareId":15,"price":0.0,"stayTime":"0","isWeb":1,"name":"木地板15","stagetypeshortname":"ymq","maxUserCount":1,"id":82,"state":"下线","doorId":"","vValue":247}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":200,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"mdb16","tipCount":0,"isHorizontal":1,"masterId":"","stagetype":"羽毛球","hardwareId":16,"price":0.0,"stayTime":"0","isWeb":1,"name":"木地板16","stagetypeshortname":"ymq","maxUserCount":1,"id":83,"state":"下线","doorId":"","vValue":246}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":398,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"mdb17","tipCount":0,"isHorizontal":1,"masterId":"","stagetype":"羽毛球","hardwareId":17,"price":0.0,"stayTime":"0","isWeb":1,"name":"木地板17","stagetypeshortname":"ymq","maxUserCount":1,"id":84,"state":"下线","doorId":"","vValue":248}}],"dayType":"nonVacations","size":0,"maxsize":0,"isContinuous":null,"continuousSize":"3","tbAppointConfigs":[{"refundPercentage":100,"shopnum":"1001","canceltime":24,"cancleTimeType":1,"appointmenttime":6,"id":2,"lastDayOpenTime":"12:00:00","timetype":0,"type":"1","ifApprove":"0","shortname":"ymq"}]}}


【拉矩阵-锁定】

GET /easyserpClient/place/getPlaceInfoByShortName?shopNum=1001&dateymd=2026-03-31&shortName=ymq&token=oy9Aj1Y7gmOS31lnOQgkXiEvgoyc HTTP/2
host: gymvip.bfsu.edu.cn
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254173b) XWEB/19027 Flue
accept: application/json, text/plain, */*
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=051uPrFa1yf9pL0DypHa1mUq5C1uPrFc&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
priority: u=1, i

返回数据
HTTP/2 200
server: nginx
date: Sun, 22 Mar 2026 14:45:28 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
content-encoding: gzip

{"msg":"success","data":{"times":["10:00","11:00","12:00","13:00","14:00","15:00","16:00","17:00","18:00","19:00","20:00","21:00","22:00"],"placeArray":[{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":6},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":3,"aAtype":0,"curUserCount":1,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq1","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":1,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球1","stagetypeshortname":"ymq","maxUserCount":1,"id":68,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":6},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":201,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq2","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":2,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球2","stagetypeshortname":"ymq","maxUserCount":1,"id":69,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":399,"aAtype":0,"curUserCount":1,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq3","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":3,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球3","stagetypeshortname":"ymq","maxUserCount":1,"id":70,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":597,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq4","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":4,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球4","stagetypeshortname":"ymq","maxUserCount":1,"id":71,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":795,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq5","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":5,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球5","stagetypeshortname":"ymq","maxUserCount":1,"id":72,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":993,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq6","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":6,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球6","stagetypeshortname":"ymq","maxUserCount":1,"id":73,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":1191,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq7","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":7,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球7","stagetypeshortname":"ymq","maxUserCount":1,"id":74,"state":"下线","vValue":30}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":6},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":1192,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq8","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":8,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球8","stagetypeshortname":"ymq","maxUserCount":1,"id":75,"state":"下线","vValue":125}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":6},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":992,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq9","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":9,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球9","stagetypeshortname":"ymq","maxUserCount":1,"id":76,"state":"下线","vValue":129}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":6},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":798,"aAtype":0,"curUserCount":1,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq10","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":10,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球10","stagetypeshortname":"ymq","maxUserCount":1,"id":77,"state":"下线","vValue":127}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":6},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":597,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq11","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":11,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球11","stagetypeshortname":"ymq","maxUserCount":1,"id":78,"state":"下线","vValue":131}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":6},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":402,"aAtype":0,"curUserCount":1,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq12","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":12,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球12","stagetypeshortname":"ymq","maxUserCount":1,"id":79,"state":"下线","vValue":133}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":6},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":198,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq13","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":13,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球13","stagetypeshortname":"ymq","maxUserCount":1,"id":80,"state":"下线","vValue":136}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":6},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":6},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":6}],"projectName":{"shopNum":"1001","hValue":4,"aAtype":0,"curUserCount":1,"stagestate":"0","tipState":0,"billNum":"","shortname":"ymq14","tipCount":0,"isHorizontal":1,"stagetype":"羽毛球","hardwareId":14,"price":0.0,"stayTime":"0","isWeb":1,"name":"羽毛球14","stagetypeshortname":"ymq","maxUserCount":1,"id":81,"state":"下线","vValue":136}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":2,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"mdb15","tipCount":0,"isHorizontal":1,"masterId":"","stagetype":"羽毛球","hardwareId":15,"price":0.0,"stayTime":"0","isWeb":1,"name":"木地板15","stagetypeshortname":"ymq","maxUserCount":1,"id":82,"state":"下线","doorId":"","vValue":247}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":200,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"mdb16","tipCount":0,"isHorizontal":1,"masterId":"","stagetype":"羽毛球","hardwareId":16,"price":0.0,"stayTime":"0","isWeb":1,"name":"木地板16","stagetypeshortname":"ymq","maxUserCount":1,"id":83,"state":"下线","doorId":"","vValue":246}},{"projectInfo":[{"oldMoney":80.0,"money":80.0,"endtime":"11:00","starttime":"10:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"12:00","starttime":"11:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"13:00","starttime":"12:00","state":4},{"oldMoney":80.0,"money":80.0,"endtime":"14:00","starttime":"13:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"15:00","starttime":"14:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"16:00","starttime":"15:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"17:00","starttime":"16:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"18:00","starttime":"17:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"19:00","starttime":"18:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"20:00","starttime":"19:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"21:00","starttime":"20:00","state":4},{"oldMoney":100.0,"money":100.0,"endtime":"22:00","starttime":"21:00","state":4}],"projectName":{"shopNum":"1001","hValue":398,"aAtype":0,"curUserCount":0,"stagestate":"0","tipState":0,"billNum":"","shortname":"mdb17","tipCount":0,"isHorizontal":1,"masterId":"","stagetype":"羽毛球","hardwareId":17,"price":0.0,"stayTime":"0","isWeb":1,"name":"木地板17","stagetypeshortname":"ymq","maxUserCount":1,"id":84,"state":"下线","doorId":"","vValue":248}}],"dayType":"nonVacations","size":0,"maxsize":0,"isContinuous":null,"continuousSize":"3","tbAppointConfigs":[{"refundPercentage":100,"shopnum":"1001","canceltime":24,"cancleTimeType":1,"appointmenttime":6,"id":2,"lastDayOpenTime":"12:00:00","timetype":0,"type":"1","ifApprove":"0","shortname":"ymq"}]}}






【getInfPmHistoryByUser】
GET /easyserpClient/place/getInfPmHistoryByUser?id=2&shopNum=1001&token=oy9Aj1Y7gmOS31lnOQgkXiEvgoyc&day=2026-03-10 HTTP/2
host: gymvip.bfsu.edu.cn
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541739) XWEB/18955 Flue
accept: application/json, text/plain, */*
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=031fl30w34wfE63VJP3w3RqbEj3fl30v&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
cookie: JSESSIONID=44A44BE982492EA91F81E5FCA12BCAE0
priority: u=1, i

HTTP/2 200
server: nginx
date: Sun, 08 Mar 2026 01:40:17 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
content-encoding: gzip

{"msg":"success","data":[{"stageCount":0,"preType":0,"shopNum":"1001","personnum":0,"premerother":"无","billNum":"0002202603051616192278540740118","prestatus":"等待","payType":"会员卡支付","stagenum":"ymq8/羽毛球8","preCloseType":"","itemorgoodname":"羽毛球","readycashnum":80.0,"readydate":"2026-03-10","readystarttime":"12:00:00","id":167367,"jsonArray":[],"preMount":0.0,"serialnumber":"xscd1001225965276","asscardnum":"0873612446","preTime":"2026-03-05 16:16:19.0","noAssDealnum":0,"readyendtime":"13:00:00","itemorgoodshortname":"ymq","phone":"13910424189","itemorgoodnum":"1.0","name":"史栋梁3W","shortName":"bjwgy10429","payStatus":0}]}

【getInfStCardByStId】
GET /easyserpClient/place/getInfStCardByStId?id=2&shopNum=1001&token=oy9Aj1Y7gmOS31lnOQgkXiEvgoyc HTTP/2
host: gymvip.bfsu.edu.cn
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541739) XWEB/18955 Flue
accept: application/json, text/plain, */*
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=031fl30w34wfE63VJP3w3RqbEj3fl30v&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
cookie: JSESSIONID=44A44BE982492EA91F81E5FCA12BCAE0
priority: u=1, i

HTTP/2 200
server: nginx
date: Sun, 08 Mar 2026 01:40:17 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
content-encoding: gzip

{"msg":"success","data":null}

【getOfferInfo获取请求】
POST /easyserpClient/common/getOfferInfo HTTP/2
host: gymvip.bfsu.edu.cn
content-length: 595
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541739) XWEB/18955 Flue
accept: application/json, text/plain, */*
content-type: application/x-www-form-urlencoded
origin: https://gymvip.bfsu.edu.cn
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=031fl30w34wfE63VJP3w3RqbEj3fl30v&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
cookie: JSESSIONID=44A44BE982492EA91F81E5FCA12BCAE0
priority: u=1, i

token=oy9Aj1Y7gmOS31lnOQgkXiEvgoyc&payMoney=200.00&shopNum=1001&projectType=3&projectInfo=%5B%7B%22day%22%3A%222026-03-09%22%2C%22oldMoney%22%3A100%2C%22startTime%22%3A%2221%3A00%22%2C%22endTime%22%3A%2222%3A00%22%2C%22placeShortName%22%3A%22ymq4%22%2C%22name%22%3A%22%E7%BE%BD%E6%AF%9B%E7%90%834%22%2C%22stageTypeShortName%22%3A%22ymq%22%7D%2C%7B%22day%22%3A%222026-03-09%22%2C%22oldMoney%22%3A100%2C%22startTime%22%3A%2221%3A00%22%2C%22endTime%22%3A%2222%3A00%22%2C%22placeShortName%22%3A%22ymq6%22%2C%22name%22%3A%22%E7%BE%BD%E6%AF%9B%E7%90%836%22%2C%22stageTypeShortName%22%3A%22ymq%22%7D%5D

HTTP/2 200
server: nginx
date: Sun, 08 Mar 2026 01:55:44 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
access-control-allow-origin: https://gymvip.bfsu.edu.cn
vary: Origin
access-control-allow-credentials: true
content-encoding: gzip

{"msg":"success","data":{"memberOffer":0.0,"xianshi":[{"oldMoney":100.0,"placeShortName":"ymq4","newMoney":100.0,"startTime":"21:00","endTime":"22:00","day":"2026-03-09"},{"oldMoney":100.0,"placeShortName":"ymq6","newMoney":100.0,"startTime":"21:00","endTime":"22:00","day":"2026-03-09"}],"memberOfferIds":[],"neibu":[{"oldMoney":100.0,"placeShortName":"ymq4","newMoney":100.0,"startTime":"21:00","endTime":"22:00","day":"2026-03-09"},{"oldMoney":100.0,"placeShortName":"ymq6","newMoney":100.0,"startTime":"21:00","endTime":"22:00","day":"2026-03-09"}],"disCounts":[],"cardsOffer":[],"dateDiscount":0.0,"dateDiscountIds":[]}}

【getUseCardInfo获取用户卡信息】
POST /easyserpClient/common/getUseCardInfo HTTP/2
host: gymvip.bfsu.edu.cn
content-length: 579
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541739) XWEB/18955 Flue
accept: application/json, text/plain, */*
content-type: application/x-www-form-urlencoded
origin: https://gymvip.bfsu.edu.cn
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=031fl30w34wfE63VJP3w3RqbEj3fl30v&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
cookie: JSESSIONID=44A44BE982492EA91F81E5FCA12BCAE0
priority: u=1, i

token=oy9Aj1Y7gmOS31lnOQgkXiEvgoyc&shopNum=1001&projectType=3&projectInfo=%5B%7B%22day%22%3A%222026-03-09%22%2C%22oldMoney%22%3A100%2C%22startTime%22%3A%2221%3A00%22%2C%22endTime%22%3A%2222%3A00%22%2C%22placeShortName%22%3A%22ymq4%22%2C%22name%22%3A%22%E7%BE%BD%E6%AF%9B%E7%90%834%22%2C%22stageTypeShortName%22%3A%22ymq%22%7D%2C%7B%22day%22%3A%222026-03-09%22%2C%22oldMoney%22%3A100%2C%22startTime%22%3A%2221%3A00%22%2C%22endTime%22%3A%2222%3A00%22%2C%22placeShortName%22%3A%22ymq6%22%2C%22name%22%3A%22%E7%BE%BD%E6%AF%9B%E7%90%836%22%2C%22stageTypeShortName%22%3A%22ymq%22%7D%5D

HTTP/2 200
server: nginx
date: Sun, 08 Mar 2026 01:55:44 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
access-control-allow-origin: https://gymvip.bfsu.edu.cn
vary: Origin
access-control-allow-credentials: true
content-encoding: gzip

{"msg":"success","data":{"koci":[],"universal":[{"delaypay":0.0,"vacaTimes":0,"weChatPriceEx":0.0,"shortcardname":"xwcw3w","vacLeftDays":0,"kafei":0.0,"cardcash":3920.0,"presentMoney":0.0,"cardindex":"0873612446","cardtime":"","shortname":"bjwgy10429","zengsongdian":0.0,"money":0.0,"cardname":"校外单位3w","identity":0,"name":"史栋梁3W","id":5759,"piliangfakaguazhang":0.0,"transLeft":0.0,"shouChuJinE":0.0}]}}

【预约场地reservationPlace】
POST /easyserpClient/place/reservationPlace HTTP/2
host: gymvip.bfsu.edu.cn
content-length: 752
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541739) XWEB/18955 Flue
accept: application/json, text/plain, */*
content-type: application/x-www-form-urlencoded
origin: https://gymvip.bfsu.edu.cn
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=031fl30w34wfE63VJP3w3RqbEj3fl30v&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
cookie: JSESSIONID=44A44BE982492EA91F81E5FCA12BCAE0
priority: u=1, i

token=oy9Aj1Y7gmOS31lnOQgkXiEvgoyc&shopNum=1001&fieldinfo=%5B%7B%22day%22%3A%222026-03-09%22%2C%22oldMoney%22%3A100%2C%22startTime%22%3A%2221%3A00%22%2C%22endTime%22%3A%2222%3A00%22%2C%22placeShortName%22%3A%22ymq4%22%2C%22name%22%3A%22%E7%BE%BD%E6%AF%9B%E7%90%834%22%2C%22stageTypeShortName%22%3A%22ymq%22%2C%22newMoney%22%3A100%7D%2C%7B%22day%22%3A%222026-03-09%22%2C%22oldMoney%22%3A100%2C%22startTime%22%3A%2221%3A00%22%2C%22endTime%22%3A%2222%3A00%22%2C%22placeShortName%22%3A%22ymq6%22%2C%22name%22%3A%22%E7%BE%BD%E6%AF%9B%E7%90%836%22%2C%22stageTypeShortName%22%3A%22ymq%22%2C%22newMoney%22%3A100%7D%5D&cardStId=289&oldTotal=200.00&cardPayType=0&type=%E7%BE%BD%E6%AF%9B%E7%90%83&offerId=&offerType=&total=200.00&premerother=&cardIndex=0873612446

HTTP/2 200
server: nginx
date: Sun, 08 Mar 2026 01:55:50 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
access-control-allow-origin: https://gymvip.bfsu.edu.cn
vary: Origin
access-control-allow-credentials: true
content-encoding: gzip

{"msg":"success","data":{"times":2,"type":"cardTimes"}}

【拉取订单getPlaceOrde】
GET /easyserpClient/place/getPlaceOrder?pageNo=0&pageSize=4&shopNum=1001&token=oy9Aj1Y7gmOS31lnOQgkXiEvgoyc HTTP/2
host: gymvip.bfsu.edu.cn
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541739) XWEB/18955 Flue
accept: application/json, text/plain, */*
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=031fl30w34wfE63VJP3w3RqbEj3fl30v&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
cookie: JSESSIONID=44A44BE982492EA91F81E5FCA12BCAE0
priority: u=1, i


HTTP/2 200
server: nginx
date: Sun, 08 Mar 2026 01:50:43 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
content-encoding: gzip

{"msg":"success","data":[{"stageCount":0,"preType":0,"shopNum":"1001","personnum":0,"premerother":"无","billNum":"0002202603080917431214758136400","prestatus":"等待","payType":"会员卡支付","stagenum":"羽毛球4","preCloseType":"","itemorgoodname":"羽毛球","readycashnum":200.0,"readydate":"2026-03-09","readystarttime":"21:00:00","infSt":{"continuousSize":"3","setPre":"羽毛球黄.jpg","shopNum":"1001","yfyj":0.0,"num":17,"businessHours":12.0,"picSizeW":100.0,"fieldimg":"beiwai2/place/badminton.jpg","shortname":"ymq","setUsed":"羽毛球绿.jpg","showUnit":1,"setFix":"羽毛球红.jpg","size":0,"price":0.0,"isWeb":1,"name":"羽毛球","picSizeH":100.0,"delayTime":"","id":2,"state":"上线","maxsize":0,"setIdle":"羽毛球绿.jpg","workStartTime":"10:00:00"},"id":167734,"jsonArray":[{"start":"21:00:00","reversionDate":"2026-03-09","siteName":"羽毛球4","end":"22:00:00"},{"start":"21:00:00","reversionDate":"2026-03-09","siteName":"羽毛球6","end":"22:00:00"}],"preMount":0.0,"serialnumber":"xscd1001475983818","asscardnum":"0873612446","preTime":"2026-03-08 09:17:43","showStatus":"0","noAssDealnum":0,"readyendtime":"22:00:00","itemorgoodshortname":"ymq","phone":"13910424189","itemorgoodnum":"2","name":"史栋梁3W","shortName":"bjwgy10429","payStatus":0},{"stageCount":0,"preType":0,"shopNum":"1001","personnum":0,"premerother":"无","billNum":"0002202603080911045636635684573","prestatus":"取消","payType":"会员卡支付","stagenum":"羽毛球4","preCloseType":"","itemorgoodname":"羽毛球","readycashnum":200.0,"readydate":"2026-03-09","readystarttime":"21:00:00","infSt":{"continuousSize":"3","setPre":"羽毛球黄.jpg","shopNum":"1001","yfyj":0.0,"num":17,"businessHours":12.0,"picSizeW":100.0,"fieldimg":"beiwai2/place/badminton.jpg","shortname":"ymq","setUsed":"羽毛球绿.jpg","showUnit":1,"setFix":"羽毛球红.jpg","size":0,"price":0.0,"isWeb":1,"name":"羽毛球","picSizeH":100.0,"delayTime":"","id":2,"state":"上线","maxsize":0,"setIdle":"羽毛球绿.jpg","workStartTime":"10:00:00"},"id":167732,"jsonArray":[{"start":"21:00:00","reversionDate":"2026-03-09","siteName":"羽毛球4","end":"22:00:00"}],"preMount":0.0,"serialnumber":"xscd1000509454220","asscardnum":"0873612446","preTime":"2026-03-08 09:11:04","showStatus":"1","noAssDealnum":0,"readyendtime":"22:00:00","itemorgoodshortname":"ymq","phone":"13910424189","itemorgoodnum":"2","name":"史栋梁3W","shortName":"bjwgy10429","payStatus":0},{"stageCount":0,"preType":0,"shopNum":"1001","personnum":0,"premerother":"无","billNum":"0002202603080154233852707049055","prestatus":"取消","payType":"会员卡支付","stagenum":"羽毛球6","preCloseType":"","itemorgoodname":"羽毛球","readycashnum":100.0,"readydate":"2026-03-09","readystarttime":"21:00:00","infSt":{"continuousSize":"3","setPre":"羽毛球黄.jpg","shopNum":"1001","yfyj":0.0,"num":17,"businessHours":12.0,"picSizeW":100.0,"fieldimg":"beiwai2/place/badminton.jpg","shortname":"ymq","setUsed":"羽毛球绿.jpg","showUnit":1,"setFix":"羽毛球红.jpg","size":0,"price":0.0,"isWeb":1,"name":"羽毛球","picSizeH":100.0,"delayTime":"","id":2,"state":"上线","maxsize":0,"setIdle":"羽毛球绿.jpg","workStartTime":"10:00:00"},"id":167731,"jsonArray":[{"start":"21:00:00","reversionDate":"2026-03-09","siteName":"羽毛球6","end":"22:00:00"}],"preMount":0.0,"serialnumber":"xscd1001778175697","asscardnum":"0873612446","preTime":"2026-03-08 01:54:23","showStatus":"1","noAssDealnum":0,"readyendtime":"22:00:00","itemorgoodshortname":"ymq","phone":"13910424189","itemorgoodnum":"1","name":"史栋梁3W","shortName":"bjwgy10429","payStatus":0},{"stageCount":0,"preType":0,"shopNum":"1001","personnum":0,"premerother":"无","billNum":"0002202603080147095287233073085","prestatus":"取消","payType":"会员卡支付","stagenum":"羽毛球4","preCloseType":"","itemorgoodname":"羽毛球","readycashnum":100.0,"readydate":"2026-03-09","readystarttime":"21:00:00","infSt":{"continuousSize":"3","setPre":"羽毛球黄.jpg","shopNum":"1001","yfyj":0.0,"num":17,"businessHours":12.0,"picSizeW":100.0,"fieldimg":"beiwai2/place/badminton.jpg","shortname":"ymq","setUsed":"羽毛球绿.jpg","showUnit":1,"setFix":"羽毛球红.jpg","size":0,"price":0.0,"isWeb":1,"name":"羽毛球","picSizeH":100.0,"delayTime":"","id":2,"state":"上线","maxsize":0,"setIdle":"羽毛球绿.jpg","workStartTime":"10:00:00"},"id":167730,"jsonArray":[{"start":"21:00:00","reversionDate":"2026-03-09","siteName":"羽毛球4","end":"22:00:00"}],"preMount":0.0,"serialnumber":"xscd1001596014973","asscardnum":"0873612446","preTime":"2026-03-08 01:47:09","showStatus":"1","noAssDealnum":0,"readyendtime":"22:00:00","itemorgoodshortname":"ymq","phone":"13910424189","itemorgoodnum":"1","name":"史栋梁3W","shortName":"bjwgy10429","payStatus":0}]}


【取消订单详情】
GET /easyserpClient/place/getCanclePlaceMoney?billNum=0002202603080917431214758136400&token=oy9Aj1Y7gmOS31lnOQgkXiEvgoyc HTTP/2
host: gymvip.bfsu.edu.cn
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541739) XWEB/18955 Flue
accept: application/json, text/plain, */*
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=031fl30w34wfE63VJP3w3RqbEj3fl30v&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
cookie: JSESSIONID=44A44BE982492EA91F81E5FCA12BCAE0
priority: u=1, i

HTTP/2 200
server: nginx
date: Sun, 08 Mar 2026 01:51:53 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
content-encoding: gzip

{"msg":"success","data":{"payMoney":200.0,"reFundMoney":200.0}}

【canclePlaceAppointmen取消预约】
POST /easyserpClient/place/canclePlaceAppointment HTTP/2
host: gymvip.bfsu.edu.cn
content-length: 121
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541739) XWEB/18955 Flue
accept: application/json, text/plain, */*
content-type: application/x-www-form-urlencoded
origin: https://gymvip.bfsu.edu.cn
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=031fl30w34wfE63VJP3w3RqbEj3fl30v&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
cookie: JSESSIONID=44A44BE982492EA91F81E5FCA12BCAE0
priority: u=1, i

outtradeno=0002202603080917431214758136400&token=oy9Aj1Y7gmOS31lnOQgkXiEvgoyc&reason=%E5%A4%A9%E6%B0%94%E5%8E%9F%E5%9B%A0


【获取卡片信息gsq】

GET /easyserpClient/card/getCardByUser?shopNum=1001&token=oy9Aj1eCxLy5xnWwRmc5eK_7GDRU HTTP/2
host: gymvip.bfsu.edu.cn
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254173b) XWEB/19027 Flue
accept: application/json, text/plain, */*
sec-fetch-site: same-origin
sec-fetch-mode: cors
sec-fetch-dest: empty
referer: https://gymvip.bfsu.edu.cn/easyserp/index.html?code=051Zp2ll24Wlqh4uWBnl2pDzcc4Zp2l3&state=123
accept-encoding: gzip, deflate, br
accept-language: zh-CN,zh;q=0.9
priority: u=1, i


HTTP/2 200
server: nginx
date: Thu, 26 Mar 2026 05:14:29 GMT
content-type: application/json;charset=UTF-8
x-application-context: easyserpClient:81
content-encoding: gzip

{"msg":"success","data":[{"vacaTimes":0,"vacLeftDays":0,"shopNum":"1001","infCs":{"cardvactiondays":0,"cardconsumfield":"0/1/2/3/4/5/6","conTimeOneWeek":0,"reduCardInte":0,"limiNum":"","cardconsumtimefield":"00:00/00:00","vacaCard":"否","cardalarmcash":0.0,"changePswSendMsg":"0","id":49,"cashfortime":0.0,"scoreValue":0.0,"vactiontime":0,"warnNum":"","cardconsumnum":0,"conTimeOneMonth":0,"ifreturncard":"否","presentMoney":0.0,"shortname":"xwtk5q","integralType":0,"cardneedcash":0.0,"enddate":0,"infC":[],"name":"校外通卡5q","useStoreInte":0.0,"isAvailable":"是","transSet":"否","sametimeconnum":0,"ifhastime":"否","cardcash":5000.0,"maxAvailableDays":0,"needValue":0,"overdueAction":0,"payBillSendMsg":"0","cardtype":"储值次卡","cardcashrate":0.0,"cardalarmtime":0.0,"kafei":0.0,"cardprice":5010.0,"minConsum":0.0,"transdate":1598544000000,"noMoneyNoRebate":"否","preSale":"0","needMoneyOne":0,"consumInteValue":0.0,"cardleveltype":"","ifAllShop":"否","addCashSendMsg":"0","ifdirect":"否"},"masterCardNum":"","cardcash":10015.0,"cardtime":"","operator":"","preActiveDate":1246464000000,"array":[],"cardstatus":"激活","cardname":"校外通卡5q","shouChuCiShu":"","identity":0,"vin":"","id":1626,"piliangfakaguazhang":0.0,"cardtype":"储值次卡","casher":"","weiXinNum":"","shouChuJinE":5000.0,"direction":"","weChatPriceEx":0.0,"delaypay":0.0,"isLabel":"","shortcardname":"xwtk5q","kafei":0.0,"batchNum":"","presentMoney":0.0,"transdate":"2009-07-02","cardindex":"2820770876","shortname":"bjwgy2043","zengsongdian":0.0,"armIp":"","activedate":"2009-07-02","cardtimePrice":"","cardfaceindex":"","money":0.0,"name":"郭素芹","transLeft":0.0,"remarks":"","cardpassword":"","formerCardNum":""}]}

