"""SMTP 发信。未配置凭据时整体降级为 no-op，不影响抓取与仪表盘。

关于 IPv6
---------
Railway 的容器解析 smtp.gmail.com 会拿到 AAAA 记录，但容器本身没有 IPv6
出口路由，于是 connect 直接抛 `[Errno 101] Network is unreachable`。
表现上很容易被误判成"密码错了"，实际上连 TCP 都没建起来。
因此这里在遇到 ENETUNREACH 时会强制解析 A 记录、按 IPv4 重连一次。
"""
from __future__ import annotations

import errno
import logging
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any, Sequence

from . import config, render

log = logging.getLogger(__name__)


def _ipv4_for(host: str) -> str | None:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    return infos[0][4][0] if infos else None


def _connect(force_ipv4: bool = False) -> smtplib.SMTP:
    """建立并完成 TLS + 登录的 SMTP 连接，调用方负责关闭。"""
    host = config.SMTP_HOST
    target = host
    if force_ipv4:
        address = _ipv4_for(host)
        if not address:
            raise OSError(errno.ENETUNREACH, f"{host} 没有可用的 A 记录")
        target = address
        log.info("按 IPv4 重连 SMTP：%s -> %s", host, address)

    context = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            target, config.SMTP_PORT, context=context, timeout=45
        )
        # 直连 IP 时证书校验要用真实主机名
        server._host = host        # type: ignore[attr-defined]
    else:
        server = smtplib.SMTP(target, config.SMTP_PORT, timeout=45)
        # starttls() 用 _host 做 SNI 与证书校验，直连 IP 时必须改回主机名
        server._host = host        # type: ignore[attr-defined]
        server.ehlo(host)
        server.starttls(context=context)
        server.ehlo(host)
    server.login(config.SMTP_USER, config.SMTP_PASSWORD)
    return server


def _connect_with_fallback() -> tuple[smtplib.SMTP, bool]:
    """先按默认方式连；遇到"网络不可达"再强制 IPv4 重试一次。"""
    try:
        return _connect(), False
    except OSError as exc:
        if getattr(exc, "errno", None) != errno.ENETUNREACH:
            raise
        log.warning("SMTP 连接遇到 ENETUNREACH（多半是 IPv6 无出口），改用 IPv4 重试")
        return _connect(force_ipv4=True), True


def _build(subject: str, text_body: str, html_body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"{config.MAIL_SUBJECT_PREFIX} {subject}".strip()
    msg["From"] = formataddr(("BIS Working Papers 监控", config.MAIL_FROM))
    msg["To"] = ", ".join(config.MAIL_TO)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def _send(msg: EmailMessage) -> tuple[bool, str]:
    """返回 (是否确认送达, 可读状态)。

    调用方必须依据布尔值决定要不要把论文标记为已报告 ——
    把失败藏在字符串里，是"发信失败却标记成功、论文永久消失"的根源。
    """
    try:
        server, _ = _connect_with_fallback()
        try:
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        # 不打印 exc 的完整堆栈到 runs.error，避免把凭据相关内容落库
        log.error("发信失败：%s: %s", type(exc).__name__, exc)
        return False, f"failed: {type(exc).__name__}: {exc}"

    recipients = ", ".join(config.MAIL_TO)
    log.info("已发送简报至 %s", recipients)
    return True, f"sent -> {recipients}"


def send_digest(papers: Sequence[dict[str, Any]]) -> tuple[bool, str]:
    if not config.EMAIL_ENABLED:
        return False, "skipped（未配置 SMTP）"
    subject = render.digest_title(len(papers))
    msg = _build(subject, render.to_markdown(papers), render.to_html(papers))
    return _send(msg)


def send_empty_notice() -> tuple[bool, str]:
    if not config.EMAIL_ENABLED:
        return False, "skipped（未配置 SMTP）"
    msg = _build(
        render.digest_title(0), render.to_markdown([]), render.to_html([])
    )
    return _send(msg)


def probe_egress() -> dict[str, str]:
    """探测容器能连出去哪些端口。

    诊断用：多数 PaaS（Railway 在内）会封禁出站 SMTP 端口来防垃圾邮件，
    表现为连接超时。这时无论密码对不对都发不出去，得换成走 443 的
    HTTP 邮件 API。把 443 也测一遍，用来区分"端口被封"和"整体没网"。
    """
    targets = [
        ("smtp.gmail.com", 587, "SMTP STARTTLS"),
        ("smtp.gmail.com", 465, "SMTP SSL"),
        ("smtp.gmail.com", 25, "SMTP 明文"),
        ("smtp.gmail.com", 2525, "SMTP 备用端口"),
        ("www.google.com", 443, "HTTPS（对照组）"),
    ]
    results: dict[str, str] = {}
    for host, port, label in targets:
        key = f"{host}:{port}（{label}）"
        address = _ipv4_for(host)
        if not address:
            results[key] = "DNS 解析失败"
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(8)
        try:
            sock.connect((address, port))
            results[key] = "✅ 可连接"
        except socket.timeout:
            results[key] = "❌ 超时（多半被封禁）"
        except OSError as exc:
            results[key] = f"❌ {exc.__class__.__name__}: {exc}"
        finally:
            sock.close()
    return results


def check_login() -> tuple[bool, str]:
    """只验证 SMTP 能否登录，**不发送任何邮件**。

    存在的理由：确认凭据是否可用，不该以打扰收件人为代价。
    这里连接、STARTTLS、AUTH 之后直接 QUIT，收件人不会收到任何东西。
    Gmail 最常见的失败是 535 —— 多半是把网页上分四组显示的应用专用密码
    连空格一起复制了，或者用了账号登录密码而非应用专用密码。
    """
    if not (config.SMTP_USER and config.SMTP_PASSWORD):
        return False, "未配置 SMTP_USER / SMTP_PASSWORD"

    used_ipv4 = False
    try:
        server, used_ipv4 = _connect_with_fallback()
        try:
            server.quit()
        except Exception:
            pass
    except smtplib.SMTPAuthenticationError as exc:
        hint = ""
        if b"535" in bytes(str(exc.smtp_code), "ascii") or exc.smtp_code == 535:
            hint = (
                "。535 通常意味着：用了账号登录密码而非应用专用密码，"
                "或应用专用密码里残留了空格，或该密码已被撤销"
            )
        return False, f"认证失败：{exc.smtp_code}{hint}"
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return False, f"连接失败：{type(exc).__name__}: {exc}"

    return True, (
        f"登录成功：{config.SMTP_USER} @ {config.SMTP_HOST}:{config.SMTP_PORT}"
        + ("（经 IPv4 回退）" if used_ipv4 else "")
        + "（未发送任何邮件）"
    )


def send_test() -> tuple[bool, str]:
    """/admin/test-email 用：验证 SMTP 配置是否可用。"""
    if not config.EMAIL_ENABLED:
        return False, "skipped（未配置 SMTP：需要 SMTP_USER / SMTP_PASSWORD / MAIL_TO）"
    body = (
        "这是一封来自 bis-wpapers-monitor 的测试邮件。\n"
        f"收件人：{', '.join(config.MAIL_TO)}\n"
        f"发信时间：{render.stamp()}\n"
    )
    msg = _build(
        "测试邮件",
        body,
        f"<p>这是一封来自 <b>bis-wpapers-monitor</b> 的测试邮件。</p>"
        f"<p>收件人：{', '.join(config.MAIL_TO)}<br>发信时间：{render.stamp()}</p>",
    )
    return _send(msg)
