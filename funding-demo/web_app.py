"""净值雷达本地启动入口。"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import List

from web_app_core import app


def lan_ip_candidates() -> List[str]:
    candidates = set()
    try:
        for value in socket.gethostbyname_ex(socket.gethostname())[2]:
            address = ipaddress.ip_address(value)
            if address.version == 4 and address.is_private and not address.is_loopback:
                candidates.add(value)
    except OSError:
        pass

    def priority(value: str):
        return (0 if value.startswith("192.168.") else 1 if value.startswith("10.") else 2, value)

    return sorted(candidates, key=priority)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    print("\n净值雷达 Demo 已启动")
    print(f"本机访问：http://127.0.0.1:{port}")
    addresses = lan_ip_candidates()
    if addresses:
        print("局域网候选地址（请选择与手机同网段的地址）：")
        for address in addresses:
            print(f"  http://{address}:{port}")
    print("按 Ctrl+C 停止。\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
