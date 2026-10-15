# Mi Health Exporter

把小米运动健康同步到本地 SQLite 和 CSV，再交给你选择的分析工具。

**纯 Python 标准库 · 无需 Root · 云端同步无需 USB · MIT**

> 我在分析自己的手环数据时，发现旧导出流程把一条上午 09:05 的 198 bpm 记录移到了次日凌晨 01:05。这个项目把同步和时间戳处理整理成一个可以检查、可以重复运行的脚本。

## 能做什么

- 从小米运动健康云端请求指定时间范围内的数据，合并到本地 SQLite；重复同步覆盖同一主键，不简单追加重复行。
- 导出 8 类 CSV：心率、睡眠、步数、血氧、压力、体重、耳机记录、运动记录。
- 高频明细使用真实 Unix 时间戳 `time`，CSV 固定输出北京时间（UTC+8）。
- 使用 Python 标准库实现认证、RC4-Drop1024、请求签名、SQLite 写入及 CSV 导出，无需 `pip install`。

这里只提供同步和导出程序；图表、自动分析报告和 AI Skill 不包含在当前版本中。

## 快速开始

需要 **Python 3.9+**。当前离线验证环境为 macOS / Python 3.13；Windows、Linux 与其他 Python 版本尚未实测。

```bash
git clone https://github.com/LilZeeCN/mi-health-exporter.git
cd mi-health-exporter
python3 sync_cloud.py --login
```

登录使用小米账号密码向 `account.xiaomi.com` 请求 `miothealth` 会话，成功后立即同步默认最近 7 天。密码不写入文件；会话凭证以**明文**保存在本地 `xiaomi_token.json`，程序在支持 POSIX 权限的系统上设置 `0600`。

如果账号要求验证码或二次验证，命令行登录可能无法完成。已有自己的 `miothealth` 会话时，可复制 `xiaomi_token.example.json` 为 `xiaomi_token.json`，填入自己的 `cUserId`、`serviceToken`、`ssecurity`；示例值不能直接使用。当前版本不提供验证码自动处理或浏览器凭证提取工具。

手环先同步到手机 App，再在电脑运行：

```bash
# 重拉最近 7 天，并合并到本地数据库
python3 sync_cloud.py

# 请求最近 30 天
python3 sync_cloud.py --days 30

# 运行离线验证：不登录、不联网、不读取你的健康数据
python3 -m unittest discover -s tests -v
```

输出目录：

```text
data/fitness_data.db
data/fitness_summary.db
analysis/steps_daily.csv
analysis/sleep_daily.csv
analysis/hr_raw.csv
analysis/spo2_raw.csv
analysis/stress_raw.csv
analysis/weight.csv
analysis/sport_records.csv
analysis/hearing_health.csv
```

`data/`、`analysis/`、凭证及备份文件已加入 `.gitignore`。仓库不含个人健康数据库、手机备份或真实会话凭证。

## 时间戳为什么容易算错

明细表中，`time` 是 UTC Unix 秒数，`timeIn0Tz = time + zoneOffsetInSec`。北京时间偏移为 28800 秒。

```python
from datetime import datetime, timedelta, timezone

beijing = timezone(timedelta(hours=8))
print(datetime.fromtimestamp(1759021500, beijing))
# 2025-09-28 09:05:00+08:00
```

导出时直接使用 `time` 并指定时区。将 `timeIn0Tz` 当成 UTC 会多偏移 8 小时；在它上面再手动加 8 小时、再按北京时间格式化，才会比正确结果多偏移 16 小时。不要把“UTC 转北京时间”本身当成 Bug。

`fitness_summary.db` 的日汇总 `timeInZero` 则用于编码自然日标签，脚本以 `timezone.utc` 格式化其日期。该处理来自当前接口和本地样本，不能直接外推到其他厂商或 App。

## 已验证与当前限制

- 这份实现来自个人小米运动健康数据同步工作流；发布检查使用合成数据验证建表、同步覆盖、删除标记、时间戳及 CSV 导出，不会调用你的账号。
- 账号密码首次登录分支尚未在本次发布中完成真实账号验证；已有会话的云端同步历史记录不能等同于首次登录已验证。
- 原始数据接口支持分页；**每日汇总当前每类最多请求 100 条，未实现分页**。`--days 365` 仅设置请求窗口，不能保证完整一年；数据保留范围也取决于云端。
- 日步数缺少汇总时，只回退聚合最近 14 天的明细；睡眠 CSV 以每天的主要睡眠为中心，不是完整的睡眠片段档案。
- “增量”指重拉最近 N 天并合并本地记录；运动记录另外保存 watermark。没有后台调度、全量历史完整性保证或导出内容自动交叉校验功能。
- CSV 使用固定 UTC+8，未处理旅行或其他时区的本地日期；设备、账号地区与 App 版本不同可能返回不同字段。
- 血氧、压力和耳机等数据是否存在取决于设备和云端记录。`high_volume` 只是接口中的 `high` 标记，不在此推定具体分贝值或损伤。
- 不附带自动生成图表或诊断功能。穿戴设备记录可以辅助复盘，不能用来确诊或排除疾病。

## 与 AI 配合

把你愿意分享的 CSV 交给 AI，或让本地编程助手读取目录。例如：

> 读取最近一周的睡眠与步数 CSV，给出按天趋势。先检查时区、缺测和字段含义；把原始记录、推断和无法判断的部分分开，不作医学诊断。

程序只向小米账号与健康服务发起请求，不包含第三方分析上传或遥测。是否将导出数据交给外部 AI 服务，由你决定。

## 项目信息

这是个人开发的非官方工具，与小米无隶属或背书关系。接口来自小米运动健康工作流，**不等同于 Zepp Life / 小米运动的通用导出器**。请使用自己的账号，合理控制同步频率。

欢迎提交问题时附 Python 版本、App 版本和去掉个人信息的错误描述；不要上传账号凭证、原始健康记录或手机备份。

[MIT License](LICENSE)
