<!--
 * @Author: Night-stars-1 nujj1042633805@gmail.com
 * @Date: 2024-03-20 22:24:35
 * @LastEditTime: 2024-07-08 23:23:31
 * @LastEditors: Night-stars-1 nujj1042633805@gmail.com
-->
# <雷索纳斯>自动跑图 - 黑月无人驾驶

>黑月锁链的最新科技，街头传闻已经出现部分商品，黑月锁链正在招募测试人员
>传说中搭载该装置的列车可以让列车长离开驾驶位时安全行车

交流群: 779434493 [![Static Badge](https://img.shields.io/badge/Tencent%20QQ-blue.svg?logo=tencentqq&logoColor=white)](https://qm.qq.com/q/OS1MxF6Rkk)


> [!WARNING]
> 该程序目前处于测试阶段

## 界面截图
![home](resources/readme/home.png)
![taj](resources/readme/taj.png)

## 使用方法
> [!TIP]
> 推荐使用**MUMU模拟器**分辨率必须为16:9，推荐: 1920x1080/1280x720
### 1. 打包程序直接运行
  - 在[releases](https://github.com/Night-stars-1/Auto_Resonance/releases/latest)下载`Auto_Resonance_xxx.zip`,如果没有就等一会儿
  - 解压在目录
  - 进入目录点击`HeiYue.exe`
### 2. 源码安装
   - 安装Python
   - 在项目根目录执行 `pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/`
   - `python gui.py`

### MuMu 多开与任务资源管理

- 在“ADB信息”中选择安装雷索纳斯的 MuMu 多开实例（例如 `#5 雷索纳斯`）；这不是多账号并行功能。未启动的实例也会显示，后台以安装路径、模拟器类型和多开 `index` 精确锁定目标。
- 其他实例可以继续运行别的游戏；程序不会启动、关闭或向它们发送游戏进程命令。
- “设置 → 任务资源管理”默认开启自动生命周期：有到期任务时自动启动所选模拟器和游戏，同一批任务只启动一次，整批结束、失败或手动停止后关闭游戏进程。
- 默认保留 MuMu 模拟器本身运行；如需进一步释放资源，可开启“队列结束后同时关闭模拟器”。
- 后台调试任务使用同一套生命周期。自定义 ADB 只支持游戏进程启停，无法自动启停未知的宿主模拟器。

## 常见问题
- `ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1006)`
  - 如果你使用了新版火绒（≥6.0），请在设置-病毒防护-web扫描中添加本程序为受信任程序，或关闭加密链接扫描功能。

## 问题反馈/功能请求
- 前往[issues](https://github.com/Night-stars-1/Auto_Resonance/issues)提交反馈或者需求

## 开发指南
- 施工中
