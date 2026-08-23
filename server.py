"""Streamable HTTP MCP server for the hide-and-seek game."""

from __future__ import annotations

import os
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from mcp_game import game_guide, start_game, take_turn


HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

mcp = FastMCP(
    "沫沫的躲猫猫",
    instructions=(
        "陪用户玩文字躲猫猫：用户是藏者，你是搜寻者。开局先调用 start_hide_and_seek；"
        "之后每次用户行动都调用 take_hide_and_seek_turn，并原样传入上一回合返回的 state_token。"
        "绝不向用户显示 state_token。根据工具结果用自然、有悬念的中文叙述行动；你可以说自己的猜测，"
        "但不要把玩家真实藏点当成搜寻者已经知道的事实。被抓到后询问是否重开。"
    ),
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
}


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_hide_and_seek_guide() -> dict[str, object]:
    """用户想了解地图、藏点或玩法，但还没有指定开局位置时使用。"""

    return game_guide()


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def start_hide_and_seek(
    hiding_room: Annotated[
        str,
        Field(description="用户选择的开局藏身房间，必须是地图中有藏点的房间。", max_length=20),
    ],
    hiding_spot: Annotated[
        str | None,
        Field(description="可选的具体藏点；不传时由游戏在该房间随机选择。", max_length=20),
    ] = None,
) -> dict[str, object]:
    """用户要开始或重新开始一局躲猫猫，并已经给出藏身房间时使用。"""

    return start_game(hiding_room=hiding_room, hiding_spot=hiding_spot)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def take_hide_and_seek_turn(
    state_token: Annotated[
        str,
        Field(description="上一回合工具结果返回的完整 state_token；必须原样传入。", max_length=16_000),
    ],
    action: Annotated[
        Literal["移动", "换藏点", "屏息", "关门", "开门", "原地不动"],
        Field(description="用户这一回合选择的动作。"),
    ],
    destination_room: Annotated[
        str | None,
        Field(description="action=移动时必填的相邻目标房间；其他动作不需要。", max_length=20),
    ] = None,
    hiding_spot: Annotated[
        str | None,
        Field(description="移动后想藏的藏点，或 action=换藏点时的新藏点。", max_length=20),
    ] = None,
) -> dict[str, object]:
    """继续正在进行的躲猫猫；执行玩家动作后，搜寻者会自动行动一回合。"""

    return take_turn(
        state_token=state_token,
        action=action,
        destination_room=destination_room,
        hiding_spot=hiding_spot,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

