import asyncio
import json
import os
import shutil

from dotenv import load_dotenv
from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from ops_agent.config import PROJECT_DIR


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise ValueError(f".env에 {name} 값을 설정해주세요.")

    return value


def create_grafana_server() -> StdioServerParameters:
    """조회 스크립트에서 공통으로 사용하는 읽기 전용 MCP 설정."""
    load_dotenv(PROJECT_DIR / ".env")

    grafana_url = required_env("GRAFANA_URL").rstrip("/")
    grafana_token = required_env("GRAFANA_SERVICE_ACCOUNT_TOKEN")

    uvx_path = shutil.which("uvx")
    if uvx_path is None:
        raise RuntimeError("uvx를 찾을 수 없습니다. PATH를 확인해주세요.")

    return StdioServerParameters(
        command=uvx_path,
        args=[
            "mcp-grafana",
            "-t",
            "stdio",
            "--disable-write",
            "--enabled-tools",
            "datasource,prometheus,loki",
        ],
        env={
            "GRAFANA_URL": grafana_url,
            "GRAFANA_SERVICE_ACCOUNT_TOKEN": grafana_token,
        },
    )


async def main() -> None:
    async with Client(create_grafana_server()) as client:
        print("1. MCP 연결 성공")

        tools = {}
        cursor = None

        while True:
            page = await client.list_tools(cursor=cursor)

            for tool in page.tools:
                tools[tool.name] = tool

            cursor = page.next_cursor
            if cursor is None:
                break

        print("\n2. 사용 가능한 도구")
        for name in sorted(tools):
            print(f"- {name}")

        if "list_datasources" not in tools:
            raise RuntimeError("list_datasources 도구가 없습니다.")

        print("\n3. Grafana 데이터소스 조회")

        result = await client.call_tool(
            "list_datasources",
            arguments={},
        )

        if result.is_error:
            for block in result.content:
                if isinstance(block, TextContent):
                    print(block.text)

            raise RuntimeError("Grafana 데이터소스 조회에 실패했습니다.")

        if result.structured_content is not None:
            print(
                json.dumps(
                    result.structured_content,
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            for block in result.content:
                if isinstance(block, TextContent):
                    print(block.text)

        print("\n4. 다음 단계에서 사용할 조회 도구의 입력 형식")

        for name in ("query_prometheus", "query_loki_logs"):
            tool = tools.get(name)

            if tool is not None:
                print(f"\n[{name}]")
                print(
                    json.dumps(
                        tool.input_schema,
                        ensure_ascii=False,
                        indent=2,
                    )
                )


if __name__ == "__main__":
    asyncio.run(main())
