# 沫沫的躲猫猫 MCP

把原版文字躲猫猫接入 ChatGPT 的 Streamable HTTP MCP 服务。用户负责藏，ChatGPT 根据铃铛、脚步声和 Bayesian belief map 自动寻找。

本项目 fork 自 [chaodeng060-source/hide-and-seek-](https://github.com/chaodeng060-source/hide-and-seek-)；原游戏逻辑与 MIT License 均保留。

## ChatGPT 中的玩法

插件提供三个工具：

- `get_hide_and_seek_guide`：查看地图、房间与藏点。
- `start_hide_and_seek`：选好房间和藏点后开局。
- `take_hide_and_seek_turn`：移动、换藏点、屏息、开关门或原地等待；ChatGPT 随后自动寻找一回合。

服务本身不保存玩家对局。每回合会返回一个压缩后的 `state_token`，ChatGPT 会在下一回合原样传回，因此 Render 休眠或重启后也不会把不同用户的对局混在一起。

## 地图与规则

- 13 个区域，AI 从客厅出发。
- 有藏点的房间才能作为开局位置。
- AI 进入同一个有藏点的房间后，还要搜中具体藏点才算抓到。
- 走廊、小阳台、大阳台没有藏点，和 AI 照面会直接被抓。
- 最多连续屏息 3 回合；继续屏息会憋不住并暴露方向。
- 小阳台的门可以关上，迫使 AI 花一回合开门；玩家穿过关着的门会发出声音。

## 本地测试

需要 Python 3.10+：

```bash
python -m unittest discover -v
```

安装依赖并启动 MCP 服务：

```bash
pip install -r requirements.txt
python server.py
```

Streamable HTTP 地址为：

```text
http://localhost:8000/mcp
```

## 部署到 Render

仓库内已经包含 `Dockerfile` 和 `render.yaml`：

1. 在 Render 新建 Blueprint。
2. 连接这个 GitHub 仓库。
3. 选择 `render.yaml` 并部署。
4. 部署成功后，MCP 地址是 `https://你的服务名.onrender.com/mcp`。
5. 在 ChatGPT 的插件页面新增自定义插件，填写该地址，身份验证选“无身份验证”。

## 文件结构

```text
hide_seek.py          原版游戏状态机、地图、藏点、移动、屏息、门和搜捕
ai_belief.py          原版 Bayesian belief map 与 AI 搜寻判断
mcp_game.py           无数据库的对局 token、玩家回合与 AI 自动行动
server.py             ChatGPT 使用的 MCP 工具
Dockerfile            Render 容器配置
render.yaml           Render Blueprint 配置
test_hide_seek.py     原版回归测试
test_mcp_game.py      MCP 封装测试
```

## License

MIT
