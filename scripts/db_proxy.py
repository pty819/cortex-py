#!/usr/bin/env python3
"""localhost TCP 代理:127.0.0.1:5433 → 192.168.1.21:5432

为什么存在:macOS 本地网络授权挡住了 uv 管理的 3.12 python 直连 192.168.1.21,
但系统 /usr/bin/python3 有 LAN 权限。所以用系统 python 跑这个透明 TCP 转发,
3.12 的 app 连 localhost:5433(localhost 不受 LAN 限制)即可到达真库。
Postgres 协议是裸字节,透明转发无需解析。

用法(系统 python,不是 uv run):
  python3 scripts/db_proxy.py            # 前台
  nohup python3 scripts/db_proxy.py &    # 后台
"""
import socket
import threading

LISTEN = ("127.0.0.1", 5433)
TARGET = ("192.168.1.21", 5432)
BUFSZ = 65536


def _set_keepalive(sock):
    """设 TCP keepalive,防止 LAN 抖动导致连接静默死亡。"""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPALIVE"):      # macOS
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 10)
    elif hasattr(socket, "TCP_KEEPIDLE"):     # Linux
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
    if hasattr(socket, "TCP_KEEPINTVL"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
    if hasattr(socket, "TCP_KEEPCNT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)


def _fwd(src, dst):
    try:
        while True:
            data = src.recv(BUFSZ)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass


def _handle(client):
    _set_keepalive(client)
    try:
        upstream = socket.create_connection(TARGET, timeout=5)
        _set_keepalive(upstream)
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        return
    threading.Thread(target=_fwd, args=(client, upstream), daemon=True).start()
    threading.Thread(target=_fwd, args=(upstream, client), daemon=True).start()


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(128)
    print(f"db-proxy listening {LISTEN[0]}:{LISTEN[1]} -> {TARGET[0]}:{TARGET[1]}", flush=True)
    try:
        while True:
            client, _ = srv.accept()
            threading.Thread(target=_handle, args=(client,), daemon=True).start()
    except KeyboardInterrupt:
        print("db-proxy stopped")


if __name__ == "__main__":
    main()
