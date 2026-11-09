# SportWorld Lab

![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![JavaScript](https://img.shields.io/badge/JavaScript-ES_modules-F7DF1E?logo=javascript&logoColor=black)
![macOS Apple Silicon](https://img.shields.io/badge/macOS-Apple_Silicon-000000?logo=apple&logoColor=white)
![Android Emulator](https://img.shields.io/badge/Android_Emulator-API_34-3DDC84?logo=android&logoColor=black)
[![GPL-3.0-only](https://img.shields.io/badge/License-GPL--3.0--only-blue)](LICENSE)
[![Checks](https://github.com/songyu00yo/sportworld-lab/actions/workflows/checks.yml/badge.svg)](https://github.com/songyu00yo/sportworld-lab/actions/workflows/checks.yml)

## 用途

SportWorld Lab 是一个本地 Android 模拟器实验控制台。它生成跑道路线与动作输入，提供离线预览、输入遥测、设备画面和中断恢复记录。

前端使用原生 HTML、CSS 和 JavaScript，后端使用 Python。控制台监听本机回环地址。公开示例采用 400 米跑道模型和合成海拔数据，默认场地范围限定在代码中的三期操场附近。

**离线检查与输入遥测分别说明模型和程序的状态，App 最终里程、步数和成绩需要单独核验。** App 的 GO、网页“开始同步”和结束提交均由使用者手动操作。

## 环境

| 项目 | 首版使用环境 |
| --- | --- |
| 主机 | macOS，Apple Silicon |
| Python | 3.14 |
| Python 依赖 | `grpcio==1.85.1` |
| Android | API 34，AOSP `arm64-v8a` 镜像 r04 |
| Java | JDK 17 或更新版本，供 Android 命令行工具使用 |
| Node.js | 24，仅用于前端测试 |
| 可选窗口工具 | `scrcpy`，也可直接使用 Android 模拟器窗口 |

## 快速开始

以下命令在 macOS 终端执行。先安装 Python 3.14，前端测试还需要 Node.js 24。

```sh
git clone https://github.com/songyu00yo/sportworld-lab.git "$HOME/sportworld-lab"
cd "$HOME/sportworld-lab"
python3.14 -m venv .lab/gps-env
source .lab/gps-env/bin/activate
python -m pip install -r requirements.txt
python -B scripts/bootstrap.py --offline
```

初始化会建立本地工作目录，并复制公开路线和海拔示例。再次执行时，已有路线与海拔文件会保留。

首次生成动作需要联网准备 MotionSense 参考数据。下载约 74 MB，程序核对固定版本、文件摘要和许可证。完成后可离线生成与验证。

```sh
python -B emulator_route.py --prepare-reference
python -B emulator_route.py --route .lab/routes/neusoft-track-route.json --verify
python -B control_panel.py --port 18763
```

浏览器会打开本地控制台。点击“生成路线与动作”，即可查看渲染跑道、图表和离线预览。缺少模拟器时，设备区域显示连接提示。`18763` 用于这个独立的离线控制台，默认完整运行入口使用 `18762`。

## 准备设备环境

这部分需要联网获取官方 Android 工具和镜像。首次准备还需要手动完成 SDK 许可确认、App 安装和登录。请先退出占用 `15556`、`15557` 或 `15037` 端口的其他 Android 环境。

### 安装 SDK 与创建平板

确认终端可以运行 `java -version`，再执行以下命令。

```sh
source .lab/gps-env/bin/activate
python -B scripts/bootstrap.py --android
/bin/sh scripts/start_android_tablet.sh --window
```

准备脚本下载固定版本的官方软件包，检查大小和摘要，创建名为 `sportworld_tablet34` 的平板。最后一条命令打开可见窗口，并持续占用当前终端。保留这个窗口和终端，后续命令在另一个终端中执行。

### 生成定位组件

在干净平板完成启动后，从这个平板提取原始定位组件，再运行补丁脚本。

```sh
cd "$HOME/sportworld-lab"
source .lab/gps-env/bin/activate
.lab/android/sdk/platform-tools/adb -P 15037 -s emulator-15556 wait-for-device
.lab/android/sdk/platform-tools/adb -P 15037 -s emulator-15556 pull \
  /vendor/bin/hw/android.hardware.gnss-service.ranchu .lab/private/gnss-original
python -B scripts/patch_gnss.py .lab/private/gnss-original
```

补丁脚本在文件偏移 `0x147cc` 处将 `0x0c` 改为 `0x8c`，检查原件与产物的 SHA-256，并生成独立产物。它接受的原件和产物摘要如下。

```text
原件  ea1ec67a7e1d4e8407b7162d49929477e142cdaf815ce59d01cb34480a10b93c
产物  4c77d8efbcab5ee685c3e40fa6553f93e0d1317698d2795c79b79465e98c2c41
```

补丁对应已检查的 `fixQuality=1` 场景，其他定位质量状态仍需验证。原始组件、生成产物和 Android 镜像均保存在本地工作目录。

### 安装 App 与登录

从官方渠道取得运动世界 App，在模拟器窗口手动安装并登录。本项目使用的包名为 `com.zjwh.android_wh_physicalfitness`，已有实现针对运动世界 App 7.4.01 的页面和组件名。微信授权流程需要使用者在官方客户端完成。

完成登录后，回到运动世界首页，再执行以下命令准备静止定位并打开控制台。

```sh
source .lab/gps-env/bin/activate
python -B runtime_manager.py start
```

已有平板窗口会在通过归属检查后复用。日常也可双击 `打开校园跑控制台.command`。`打开测试平板.command` 请求额外的镜像窗口，需要先安装可选工具 `scrcpy`。停止输入及关闭后台使用控制台中的对应按钮，或双击 `彻底退出校园跑.command`。

## 路线配置与操作顺序

本地路线位于 `.lab/routes/neusoft-track-route.json`。公开示例包含跑道几何和空的 `checkpoint_markers`，适合离线预览与户外测试准备。

校园模式还需要配置 App 当天实际下发的点位。将坐标按 `[经度, 纬度]` 写入 `checkpoint_markers`，填写 3～20 个不同点位，并保留点位来源说明。点位位置应来自使用者核对过的 App 地图。示例中的空点位会阻止校园模式继续准备。

路线校验要求顶点位于 `STADIUM_SEED` 周围 600 米内，400 米内圈模型也受这个边界约束。修改本地 JSON 后，应重新生成计划并核对地图。

生成路线与动作并确认检查结果后，按所选模式操作。

- 校园模式先核对当天点位和定位状态，在 App 开始跑步。倒计时结束后，由使用者点击网页“开始同步”。收到提示时在 App 暂停、核对距离，再手动结束并提交。
- 首次户外测试先保持 App 在户外跑首页，由使用者点击网页“开始预跑”。预跑 8 秒后，按提示手动点击 App 的 GO。结束前 12 秒会提示暂停 App，并长按结束、确认。剩余输入结束后，程序恢复传感器。
- 户外续跑在 App 点击 GO，跑步页面出现后，由使用者确认同步。具体按钮与顺序以当前步骤提示为准。

结束后，将 App 显示的实际结果登记到本地记录。

单次输入上限为 1.2 公里和 300 秒。中断后的恢复与结果核对依赖本地记录，请保留 `private/`。

## 测试

初始化公开示例后运行离线回归测试。

```sh
source .lab/gps-env/bin/activate
python -B -m unittest discover -s . -p 'test_*.py'
node --test test_progress.mjs test_workflow.mjs
```

设备命令、传感器输入和工作流测试使用替身。MotionSense 缓存集成测试在参考数据缺失时跳过，执行 `--prepare-reference` 后会运行。GitHub Checks 准备固定公开参考数据后，运行离线测试和启动脚本语法检查。

## 已知限制

- 全新平板仍需要使用者安装 App、登录并核对定位。自动检查覆盖离线模型和程序行为，完整跑步验收以使用者手动完成后的 App 结果为准。
- 公开海拔文件是合成数据。旧版截图提取数据仍可读取，来源在报告中保留。示例路线的尺寸属于模型设定，现场位置与 App 打卡范围需要另行核对。
- 当前界面存在运行记录与检查面板重叠、操作提示遮挡遥测、禁用原因隐藏的问题。文字与设备画面偏小，速度图图例的深浅说明写反，空格快捷键可能抢占按钮操作。
- 参考动作来自有限的公开样本。数学一致性和样本相似度各自有适用范围，App 计步、里程与服务端判断需要实际验证。

## 文件与第三方来源

公开仓库包含核心源码、前端、测试、启动器、环境准备脚本和两份示例。程序将下载内容与生成文件写入 `.lab/`，本地运行记录写入 `private/`。这两个目录已加入 Git 忽略规则。

| 来源 | 用途与许可 |
| --- | --- |
| [MotionSense](https://github.com/mmalekzadeh/motion-sense) | 动作参考数据，MIT。固定版本与摘要见 `motion_reference.py`，下载时保留原许可证。 |
| [Android SDK 与 Emulator](https://developer.android.com/studio/run/emulator) | 官方工具和系统镜像由准备脚本单独下载，适用各自许可。版本、地址和摘要见 `scripts/setup_android.py`。 |
| [AOSP Goldfish](https://android.googlesource.com/device/generic/goldfish/) | 模拟器设备实现的上游源码。定位原件从固定官方镜像取得，补丁逻辑见 `scripts/patch_gnss.py`。 |
| [gRPC Python](https://github.com/grpc/grpc) | 通过本机官方模拟器接口更新定位，Apache-2.0。 |
| [scrcpy](https://github.com/Genymobile/scrcpy) | 可选设备镜像工具，Apache-2.0。 |

## 许可证

本项目原创代码采用 **GPL-3.0-only**。Copyright © 2026 songyu00yo。

分发受本协议约束的修改版本时，需要向接收者提供对应源码，并按 GPL-3.0-only 条款分发。第三方工具和数据适用各自许可证。完整条款见 [LICENSE](LICENSE)。
