"""对外 HTTP 请求前的 URL 安全校验。

仅允许 http/https；host 必须解析到公网地址，拒绝环回 / 私有 / 链路本地 /
保留 / 组播段，防止把请求（连同可能的凭据）打向内网。供 fast_brain /
mimo_tts 这类读取用户配置端点的模块共用。
"""
import ipaddress
import socket
import urllib.parse


def validate_public_http_url(url: str) -> str:
    """校验 url 可安全对外请求；通过则原样返回，不通过抛 ValueError。"""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https，得到: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL 缺少 host: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port)
    except OSError as e:
        raise ValueError(f"host 解析失败: {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"host {host} 解析到非公网地址 {ip}，已拒绝请求")
    return url
