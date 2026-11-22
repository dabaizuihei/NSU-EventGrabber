# NSU-EventGrabber · 成都东软学院素质活动监控与自动报名

针对东软智慧教育平台（`study.nsu.edu.cn`）的素质活动监控与自动报名工具。定时扫描活动广场，发现新活动立即推送提醒，按规则自动报名，已满额活动有人数释放时即刻通知。

## 免责声明

- 本工具只做「监控 + 提交报名」，不绕过任何限制、不破解任何机制。使用者需遵守学校相关规定，因使用本工具产生的一切后果由使用者自行承担。
- 自动报名会以使用者账号执行真实报名操作，请自行评估账号风控风险。`credentials.json` 含账号凭据，不要分享给他人，也不要提交到公开仓库。

## 已实现功能

- [x] 一键登录：内置系统 WebView，滑块手动拖一次，登录后保存 token。
- [x] 自动续期：每轮扫描前检查 token，失效时刷新；refresh token 也失效就走登录窗口。
- [x] 通知：系统通知和 Webhook。Webhook 支持 Server酱、PushPlus、Bark、Telegram、企业微信机器人等。
- [x] 自动报名：报名时间未到时先挂起，开放后的第一轮扫描补报一次；命中屏蔽词、名额已满或历史报过的活动仍会跳过。

## 快速开始

前往Releases下载：<https://github.com/dabaizuihei/NSU-EventGrabber/releases/latest>

- Windows：下载 `NSU-EventGrabber-Windows-x86_64.exe`
- macOS Apple Silicon：下载 `NSU-EventGrabber-macOS-arm64.dmg`。
- macOS Intel：下载 `NSU-EventGrabber-macOS-x86_64.dmg`。

  打开 DMG 后把 `.app` 拖进「应用程序」。提示「无法验证开发者」时，在 Finder 里右键选择「打开」。
- Linux：下载 `NSU-EventGrabber-Linux-x86_64.AppImage`，赋予执行权限后运行。系统需要 WebKitGTK：
  ```bash
  # Ubuntu 24.04
  sudo apt install python3-gi gir1.2-webkit2-4.1
  # Ubuntu 22.04
  sudo apt install python3-gi gir1.2-webkit2-4.0
  # 可选：系统托盘（没有该库时仍可使用，但关闭按钮会直接退出）
  sudo apt install gir1.2-ayatanaappindicator3-0.1
  ```

从源码运行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python3 gui_launcher.py
```

Windows 用 `.venv\Scripts\activate` 激活。

## 通知配置

在设置页「通知」标签里选择渠道：

| 渠道      | 说明                                                   | 依赖               |
| ------- | ---------------------------------------------------- | ---------------- |
| Windows | Windows 10/11 Toast                                  | 系统自带 PowerShell  |
| macOS   | 通知中心                                                 | 系统自带 `osascript` |
| Linux   | `notify-send`，缺失时用 `zenity`                          | 发行版自带            |
| Webhook | Server酱 / PushPlus / Bark / Telegram / 企微 / 自定义 JSON | 标准库              |

Webhook 类型选「自动识别」即可。常用地址：

- Server酱：`https://sctapi.ftqq.com/<SendKey>.send`
- PushPlus：`https://www.pushplus.plus/send?token=<token>`
- Bark：`https://api.day.app/<key>`
- Telegram：`https://api.telegram.org/bot<token>/sendMessage?chat_id=<chat_id>`
- 企业微信机器人：`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>`

## 项目结构

```text
.
├── .github/
│   └── workflows/
│       └── build.yml              GitHub Actions 四平台构建与 Pages 发布
├── gui/
│   ├── __init__.py
│   ├── app.py                    GUI 控制器、扫描调度与 pywebview 桥
│   └── web/
│       ├── app.js                主界面状态轮询与交互
│       ├── index.html            主界面
│       ├── NSU-EventGrabber.png  界面 Logo
│       └── style.css             界面样式
├── notify/
│   ├── __init__.py
│   ├── base.py                   通知分发
│   ├── desktop.py                系统通知
│   └── webhook.py                Webhook 通知
├── packaging/
│   ├── build_macos_dmg.sh        macOS DMG 打包
│   ├── dmg-background.png        DMG 背景
│   ├── NSU-EventGrabber.desktop  Linux 桌面入口
│   ├── NSU-EventGrabber.icns     macOS 图标
│   ├── NSU-EventGrabber.ico      Windows 图标
│   ├── NSU-EventGrabber.png      安装包图标
│   ├── NSU-EventGrabber.spec     PyInstaller 配置
│   └── version_info.txt          Windows 版本信息
├── tests/
│   ├── __init__.py
│   ├── test_auto_register.py     自动报名与开放后补报
│   ├── test_config.py            配置加载
│   ├── test_login_state.py       登录态与子进程
│   ├── test_single_instance.py   单实例锁与唤醒
│   ├── test_upcoming.py          参加提醒默认值与 0 天关闭
│   ├── test_updater.py           版本比较
│   └── test_window_lifecycle.py  托盘恢复窗口
├── .gitattributes
├── .gitignore
├── auth.py                       凭据、登录态、token 刷新
├── client.py                     平台接口
├── config.example.env            配置示例
├── config.py                     配置加载与默认值
├── gui_launcher.py               启动入口，同时负责登录子进程
├── LICENSE                       MIT License
├── login_webview.py              独立登录窗口
├── netutil.py                    系统 CA / TLS
├── pyproject.toml                项目与打包依赖
├── README.md
├── scanner.py                    扫描、过滤、自动报名
├── single_instance.py            单实例文件锁与显示请求
├── state.py                      本地活动状态
├── tray.py                       系统托盘与后台常驻
├── updater.py                    版本检查
└── version.py                    版本号与仓库地址
```

## 常见问题

| 现象          | 处理                                                                |
| ----------- | ----------------------------------------------------------------- |
| 登录窗口没弹出来    | 检查系统安全软件；Linux 确认已装 WebKitGTK                                     |
| 一直提示登录失效    | token 过期，点「登录」重新登一次                                               |
| 报名提示人数已达上限  | 平台并发限制，本地只能预检，最终以系统为准                                             |
| 关闭窗口后程序还在运行 | 默认最小化到托盘；从托盘菜单选「退出」才会结束进程                                         |
| 托盘图标不显示     | Windows/macOS 检查系统托盘权限；Linux 安装 `gir1.2-ayatanaappindicator3-0.1` |

## 注意事项

1. 扫描间隔别太低，建议不低于 60 秒，避免触发平台风控。
2. 自动报名成功只代表请求提交成功，最终结果以平台显示为准。
3. `credentials.json` 含账号凭据，不要分享，也不要提交到公开仓库。
4. 删除 `state.json` 会重建基线；首次或重建基线只记录存量活动，不推送也不自动报名，下一轮扫描才开始按规则提醒。
