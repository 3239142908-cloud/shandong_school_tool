# 闪动校园 8.7.7 — 本地部署说明

本目录已完成部署（源码修复 + 依赖就绪 + 启动器就位）。

## 一键启动

**双击桌面上的「闪动校园 8.7.7」快捷方式**（或本目录内的 `启动.cmd`），会打开一个中文菜单控制台：

```text
  [1]  冒烟自检（离线，验证代码完整性）
  [2]  账号登录
  [3]  凭证管理（查看 / 注入 / 清除）
  [4]  我的资料与学生卡
  [5]  跑步业务（阳光跑 / 自由跑）
  [6]  跑步历史与跑单详情
  [7]  成绩 / 体测 / 考试 / 考勤
  [8]  排行榜
  [9]  每日任务 / 福利大厅
  [10] 人脸认证 / OSS 令牌
  [11] AI 室内运动结算
  [12] 社区动态
  [13] 校友专区
  [14] 消息中心
  [15] 跑步方案与电子围栏
  [16] 后端连通性检测
  [17] 全部工具帮助
  [0]  退出
```

输入序号回车即可；每次执行都会打印**等价命令行**，方便你直接复制到终端复用。

## 快速开始（命令行）

```cmd
cd /d <PROJECT_DIR>
run.cmd --list
run.cmd smoke_test                                  :: 冒烟自检
run.cmd login_cli --help
run.cmd login_cli -u <手机号> -p <密码>              :: 账号登录（触发滑块人机校验）
run.cmd community_cli --help
```

## 跑步全链路（本版本新增）

`run_cli.py` 已接入轨迹引擎，从"只发起起跑"升级为**起跑 → 分片上传 → 结算**完整链路。

```cmd
:: 1) 只生成数据、不发任何请求（先看会提交什么）
run.cmd run_cli --dry-run -t sun -d 2000 -p 360 --seed 42 --out run.json

:: 2) 真实执行阳光跑
run.cmd run_cli -t sun -d 2000 -p 360 --fence fence.json

:: 3) 自由跑
run.cmd run_cli -t free -d 1600 -p 380 --lat 36.67 --lng 117.05
```

`--fence` 是围栏点位 JSON（`[[lat,lng],...]` 或 `{"points":[[lat,lng],...]}`）。
不传时会尝试从 `querySchoolFences` 接口解析；再不行则用 `--lat/--lng` 构造一个
520m 见方的矩形环路，最后才回退到内置默认围栏。

### 轨迹引擎 `trajectory_engine.py`

替代 `running_protocol.py` 里那个 `raise NotImplementedError` 的空实现。

**核心设计：所有输出由同一个速度序列 v(t) 推导**，所以任意两路数据交叉验证都自洽：

```
v(t) ──积分──> s(t) ──积分──> 步数(t)
 │              │                │
 │              └─> paceList 距离  └─> stepList 步数
 └─> 步频(t) ──> 步幅(t) = v·60/cadence
```

已实现的抗风控特性（全部有自检断言）：

| 项 | 做法 | 自检实测 |
| :--- | :--- | :--- |
| 配速非常数 | 疲劳 U 型 + 1/f 慢漂移 + 逐点微抖 + 4–12 次低频加减速事件 | 变异系数 0.32 |
| 坐标非等距 | 由 v(t) 积分得到 | 相邻位移 σ=3.17 m |
| accuracy 非常数 | Gamma 采样 + 偶发 >50m 尖峰 | 均值 9.4 m，σ=9.3 m，最大 79 m |
| stability 非常数 | 由 accuracy 概率化推导 | 319×1 / 16×0 |
| 采样非等间隔 | 1000–3000ms 抖动 + 6% 漏采 | 间隔 σ=824 ms |
| 步频/步幅/配速闭合 | cadence=93.4·v^0.4683，stride=v·60/cadence | 步幅 1.10–1.19 m |
| 围栏内闭合 | 沿围栏法向缓冲区绕行，GPS 抖动越界自动衰减 | 越界 0 点 |
| 距离可控 | 时长二分 + 基础速度回标（两轮联合标定） | 2000 m 目标实到 2000 m |
| 步数一致 | stepList / strideList / totalStep 三者收口 | 误差 0.000 |
| 合理暂停 | 0–3 次固定长度窗口，暂停内位移与步数同时冻结 | 1 段 / 44 s |

自检：`python trajectory_engine.py --selftest`（约 0.12 s，全部断言通过）
生成：`python trajectory_engine.py -d 2000 -p 360 --fence fence.json --out run.json`

### 设备身份（`device_identity.py`）

- 机型池 28 组真机机型，每机型绑定合法 Android 11–14 版本
- `deviceId` 首次生成后写入 `session.json` **永久锁定**，不再每次运行随机变化
- 环境变量覆盖优先级最高：`SHANDONG_DEVICE_ID` / `SHANDONG_MODEL` / `SHANDONG_SYSTEM_VERSION`
- 自检：`python device_identity.py selftest`

### 请求节流（`transport.py`）

- 请求间隔默认随机 0.4–2.5 s（`SHANDONG_THROTTLE=0` 关闭，`SHANDONG_THROTTLE_RANGE=lo,hi` 自定义）
- 瞬时失败（网络异常 / 限流 / 网关抖动）指数退避重试，最多 3 次
- 跑前浏览行为注入（可 `--no-pre-behavior` 关闭）

## 本次部署修了什么

上游发布时把开发机路径和转义序列一起打包了，源码原样无法运行。已修 4 个文件、5 处：

| 文件 | 缺陷 | 修复 |
| :--- | :--- | :--- |
| `smoke_test_877.py` | 写死 `cur_dir = r"D:/Work/闪动校园/协议/8.7.7"`，工具集检查恒为 0/13 | 改为脚本自身目录 |
| `transport.py` | ADB 回退路径写死 `D:\Work\Tools\Android\...`，`\a`/`\b` 被还原成 BEL 控制字符 (0x07) | 按 `ADB_PATH` → `ANDROID_SDK_ROOT` → `ANDROID_HOME` → `%LOCALAPPDATA%\Android\Sdk` 探测 |
| `transport.py` | 未关闭 `requests` 系统代理自动探测，本机 WinINET 代理 `127.0.0.1:16385` 会把请求挂起 12s | `self.session.trust_env = False`（`SHANDONG_TRUST_ENV=1` 可恢复；`--proxy` 始终生效） |
| `community_cli.py` | 3 处 `.replace("\n"," ")` 的 `\n` 变成真实换行 → `SyntaxError`，CLI 起不来 | 还原为 `"\n"` 字面量 |
| `alumni_cli.py` | 同上（1 处） | 还原为 `"\n"` 字面量 |

原始文件已备份在 `_backup_upstream\`，需要回溯上游原貌时直接覆盖回来。

## 反检测层（真机 HAR 逐项对齐）

所有数值都来自真实 iPhone 2.0.6 抓包（`capture_sun.har`）与 6 单实测验证。

### 请求头

上游额外发了 4 个真机不存在的头，已全部移除：

| 头 | 真机 | 上游 | 处置 |
| :--- | :--- | :--- | :--- |
| `timestamp` | 无（只在请求体里） | 有 | 删除 |
| `token` | 无（只有 `Authorization`） | 有 | 删除 |
| `v` | 无 | 有 | 删除（参数保留以兼容调用） |
| `api` | 无 | 有 | 删除（同上） |

补齐真机必带的 6 个头：

```
Accept: */*
Accept-Encoding: gzip, deflate
Accept-Language: zh-Hans-CN;q=1, de-CN;q=0.9
Connection: keep-alive
Content-Type: application/json          ← 无 charset 后缀
Cookie: cna=<32位hex>                    ← 由 deviceId 派生，每设备恒定
e: 0
```

`captcha/get` / `captcha/check` 额外补 WebView 来源头（真机滑块在 H5 页里做）：

```
Origin: https://mh5.huachenjie.com
Referer: https://mh5.huachenjie.com/
Sec-Fetch-Dest: empty
Sec-Fetch-Mode: cors
Sec-Fetch-Site: same-site
```

### 请求体

键顺序、字段集、类型全部对齐：

```json
{"deviceId":"...","buildVersion":"1","appVersion":"2.0.6","platform":1,
 "appCode":"SDPRO001","systemVersion":"26.6.2","modelName":"iPhone 17 Pro Max",
 "timestamp":"1792234830553"}
```

- 真机**没有** `channel` 字段 → 已去掉
- `platform` 是**整数**不是字符串 → 已转 int
- 键顺序决定签名文本，必须逐字节一致

### TLS 指纹

`vendor/curl_cffi`（6.4 MB）提供真机级 TLS 握手：
- iOS 身份 → `impersonate=safari`
- Android 身份 → `impersonate=chrome`（curl_cffi 当前版本不支持 okhttp）
- 不可用时自动回退 `requests`；`SHANDONG_TLS_IMPERSONATE=0` 可关闭

### 滑块

从 HAR 解出真机明文：`{"x":184.93569131832797,"y":15}`

| 项 | 真机 | 上游 | 现状 |
| :--- | :--- | :--- | :--- |
| 密钥 | `SLIDER_KEY` | `SLIDER_KEY` | ✅ 一致 |
| `y` | 整数 `15` | `15.0` | ✅ 已转 int（`15.0` 会让密文不同） |
| `x` | 浮点，无偏移 | `int(x-6.5)+16.5` | ✅ 改为原值直传 |

密文已与真机逐字节一致：

```
真机: Mf165+k2LtxuP48ZoU9U0xMLg5OdjPmbih7AjUD7SGo=
我们: Mf165+k2LtxuP48ZoU9U0xMLg5OdjPmbih7AjUD7SGo=
```

另修：`/captcha/check` 返回 `code=0` 但 `data.result=false` 属于失败，上游只判断 `code` 会误判通过。

### 行为序列

跑前按真机路径走 9 个请求（带 0.6~6s 随机停顿，总耗时约 20s）：

```
消息红点 → 社区推荐流 → 帖子详情 → 上报浏览 → 帖子详情 → 上报浏览
        → 里程排行榜 → 个人资料 → 学校跑步概览
```

### 设备身份

支持两套真机画像，`session.json` 驱动：

| 字段 | iOS Pro | Android 标准 |
| :--- | :--- | :--- |
| `deviceId` | 40 位 hex | 16 位 hex |
| `modelName` | `iPhone 17 Pro Max` | `Samsung\|SM-A5560` |
| `appCode` | `SDPRO001` | `SD001` |
| `platform` | 1 | 2 |
| `appVersion` | 2.0.6 | 8.7.7 |
| UA | `ShanDong/2.0.6 (iPhone 17 Pro Max; iOS 26.6.2; Scale/3.00)` | `ShanDong/8.7.7 (Samsung;Android 12)` |

机型池 29 组真机机型，含 `Samsung|SM-A5560`（版本 12/13/14）。

## 一键开跑（桌面快捷方式）

桌面上已创建 **「闪动校园 一键开跑」**，双击即跑一单自由跑。

脚本：`一键开跑.cmd`

**流程**（三步，全程约 8 分钟）：

```
[1/3] 检查登录状态        只读 probe，token 失效会直接提示并停下
[2/3] 起跑 + 等待真实时长  按 duration 真实等待，每 60 秒打印进度
[3/3] 结算并显示结果
```

**改参数**：编辑 `一键开跑.cmd` 顶部四行，保存即可：

```cmd
set "DIST=1000"          目标距离（米）
set "PACE=480"           平均配速（秒/公里）
set "LAT=<你的纬度>"       起跑纬度
set "LNG=<你的经度>"     起跑经度
```

**命令行测试模式**（双击不会触发）：

```cmd
一键开跑.cmd probe      只读检查登录状态，不创建跑单
一键开跑.cmd dry        只生成轨迹数据，不联网
```

**注意**：双击 = 真实提交跑单，会产生一条记录。只想检查登录状态时用 `probe`。

## 运行环境

- 解释器：`<PYTHON_EXE>`（CPython 3.12.2）
- 依赖：pycryptodome 4.0.0 / requests 2.34.2 / numpy 2.5.4 / opencv-python-headless 5.0.0.93
  （numpy + opencv 供 `captcha_solver.py` 做滑块缺口识别，缺失时自动降级为固定偏移 120.0）
- `run.cmd` 默认使用上面这个 venv；换机器时设 `SHANDONG_PYTHON` 环境变量指向新解释器即可。

## 验收结果

```text
[冒烟] desktop-8.7.7 -> 通过     # 密码学闭环 / 会话模板 / 客户端初始化 / 75 接口完备 / 13 工具就绪 / 脱敏审计
[导入] desktop-8.7.7 核心模块 x7 -> 通过
[CLI ] desktop-8.7.7 入口 x13 -> 全部可用
[通过] 15 项检查全部通过
```

后端实测：`https://api.huachenjie.com/run-front/` 返回 `HTTP 404 {"code":1516,"message":"请登录后操作！"}`，
即请求已到业务网关、因缺凭证被正常拒绝 —— 签名、TLS、传输链路全通。

## 注意

1. `session.json` 仍是占位模板（`YOUR_TOKEN_HERE`），填入真实 token/satoken 后才能联网调业务接口。
   设备身份字段（`deviceId`/`modelName`/`systemVersion`）**不要手工填**，首次运行会自动生成并永久锁定。
2. `run_cli.py` 已接入轨迹引擎，可完成起跑 → 上传 → 结算全链路。
   先用 `--dry-run` 检查生成的数据，确认无误再去掉该参数真实执行。
3. 8.7.2 / 8.7.4 / 8.7.7 协议互不兼容，本目录只对应 8.7.7 客户端。
4. 仅限自有账号与已授权环境做协议研究；上游 README 的免责声明仍然适用。
