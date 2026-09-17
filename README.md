# Tuoz1 Companion

[Tuoz1 Bot](https://tuoz1bot.com/) 的客户端插件。运行在你自己的电脑上，读取英雄联盟客户端的本地接口（LCU），
在每局游戏结束后把战绩发送给 Discord 机器人，让 **海克斯大乱斗** 等 Riot 公开 API 拿不到的模式也能正常播报、评分。

只依赖 Python 标准库，没有任何第三方包。不会修改游戏，不会读取账号密码。

## 下载

最新版本： **[Tuoz1Companion.exe](../../releases/latest/download/Tuoz1Companion.exe)**

## 使用方法

1. 在 Discord 里先 `/bind` 绑定账号，再输入 `/companion_token`，机器人会私下回复 **令牌**。
2. 双击运行 `Tuoz1Companion.exe`，按提示粘贴令牌，只需输入一次（保存在同目录的 `companion_config.json`）。
   机器人地址已经内置在插件里，不需要也不能自己填。
3. 打游戏时保持插件窗口开着。打完一局后，插件会等客户端生成战绩（通常几十秒），然后自动上报。
4. 和平时一样，待在语音频道里机器人才会播报。打完时不在语音也没关系，30 分钟内进语音会自动补播。

程序没有数字签名，Windows 可能提示"未知发布者"，点"仍要运行"即可。不放心的话可以直接用源码运行：

```bash
python tuoz1_companion.py
```

需要 Python 3.10 或更新版本。

## 常用参数

```bash
python tuoz1_companion.py --send-latest    # 启动后立刻把最近一场比赛上报一次（测试用）
python tuoz1_companion.py --reset          # 重新输入令牌
python tuoz1_companion.py --dump-dir dump  # 把上报的数据另存一份（排查问题用）
python tuoz1_companion.py -v               # 详细日志
```

日志写在同目录的 `companion.log`。

从 v1.2.0 起插件会自动更新：启动时和每 6 小时检查一次新版本，自动下载替换并重启，配置和令牌不受影响。不想自动更新加 `--no-update`。

## 工作原理

```
你的电脑                                        机器人服务器
┌───────────────┐   本机 HTTPS    ┌──────────────┐   HTTP POST   ┌──────────────┐
│ 英雄联盟客户端 │ ◄───────────── │ Tuoz1Companion│ ────────────► │  Tuoz1 Bot   │
│ (LCU API)     │  战绩 / 赛后统计 │               │               │ 分析 → 播报   │
└───────────────┘                 └──────────────┘               └──────────────┘
```

- 通过客户端进程的命令行参数（或 lockfile）拿到本机接口的端口和密码，和 LeagueAkari 等工具原理相同。
- 对局进行中记录 gameId，结束后读取 `/lol-match-history/v1/games/{gameId}` 拿完整战绩，
  并在结算阶段读取 `/lol-end-of-game/v1/eog-stats-block` 补上治疗队友、护盾队友等字段。
- 只上报比赛数据和当前账号的 puuid，令牌只用于向机器人证明"这是我"。

## 自己打包

```bash
pip install pyinstaller
pyinstaller --onefile --console --name Tuoz1Companion tuoz1_companion.py
```

或者直接运行 `build.bat`，产物在 `dist/Tuoz1Companion.exe`。

## 许可

MIT
