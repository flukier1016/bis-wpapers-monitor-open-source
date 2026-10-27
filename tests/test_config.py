"""配置解析测试。"""
from __future__ import annotations

import importlib

import pytest


def _reload(monkeypatch: pytest.MonkeyPatch, **env: str):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from app import config

    return importlib.reload(config)


class TestSmtpPassword:
    """Gmail 应用专用密码的分组显示格式必须被容错处理。

    Google 网页上按 `abcd efgh ijkl mnop` 分四组显示 16 位密码，照着复制
    就会带上 3 个空格，而 Gmail 的 SMTP 不接受这种形式，只会返回 535 认证
    失败。变量"看上去"完全正常，排查起来很费时间。
    """

    def test_gmail_grouped_format_is_normalised(self, monkeypatch) -> None:
        cfg = _reload(monkeypatch, SMTP_PASSWORD="abcd efgh ijkl mnop")
        assert cfg.SMTP_PASSWORD == "abcdefghijklmnop"

    def test_already_stripped_is_untouched(self, monkeypatch) -> None:
        cfg = _reload(monkeypatch, SMTP_PASSWORD="abcdefghijklmnop")
        assert cfg.SMTP_PASSWORD == "abcdefghijklmnop"

    def test_password_with_legitimate_spaces_is_preserved(self, monkeypatch) -> None:
        """别的邮件服务允许密码里有空格，不能一律剥离。"""
        cfg = _reload(monkeypatch, SMTP_PASSWORD="correct horse battery staple!")
        assert cfg.SMTP_PASSWORD == "correct horse battery staple!"

    def test_near_miss_shapes_are_preserved(self, monkeypatch) -> None:
        for value in (
            "abc efgh ijkl mnop",        # 首组只有 3 位
            "abcd efgh ijkl",            # 只有 3 组
            "abcd efgh ijkl mnop qrst",  # 5 组
            "abcd  efgh ijkl mnop",      # 双空格
            "ab-d efgh ijkl mnop",       # 含非字母数字
        ):
            cfg = _reload(monkeypatch, SMTP_PASSWORD=value)
            assert cfg.SMTP_PASSWORD == value, f"{value!r} 被误改"

    def test_surrounding_whitespace_is_trimmed(self, monkeypatch) -> None:
        cfg = _reload(monkeypatch, SMTP_PASSWORD="  secret  ")
        assert cfg.SMTP_PASSWORD == "secret"

    def test_email_enabled_requires_all_three(self, monkeypatch) -> None:
        cfg = _reload(
            monkeypatch, SMTP_USER="a@b.com",
            SMTP_PASSWORD="abcd efgh ijkl mnop", MAIL_TO="x@y.com",
        )
        assert cfg.EMAIL_ENABLED is True

        cfg = _reload(monkeypatch, SMTP_PASSWORD="")
        assert cfg.EMAIL_ENABLED is False


class TestTimezone:
    def test_bad_timezone_falls_back(self, monkeypatch) -> None:
        """时区拼错不该让整个服务起不来。"""
        cfg = _reload(monkeypatch, TIMEZONE="Not/AZone")
        assert cfg.TIMEZONE == "Asia/Shanghai"

    def test_valid_timezone_kept(self, monkeypatch) -> None:
        cfg = _reload(monkeypatch, TIMEZONE="America/New_York")
        assert cfg.TIMEZONE == "America/New_York"


class TestMailTo:
    def test_splits_on_comma_and_semicolon(self, monkeypatch) -> None:
        cfg = _reload(monkeypatch, MAIL_TO=" a@x.com , b@y.com ; c@z.com ")
        assert cfg.MAIL_TO == ["a@x.com", "b@y.com", "c@z.com"]

    def test_empty_is_empty_list(self, monkeypatch) -> None:
        cfg = _reload(monkeypatch, MAIL_TO="")
        assert cfg.MAIL_TO == []


class TestNormalisationFlag:
    """归一化标志供启动日志使用。

    config 是在 import 阶段求值的，那时 logging.basicConfig() 还没跑，
    直接在 config 里打 INFO 日志会被根 logger 丢掉 —— 诊断信息不可见
    等于没有。所以改成置标志、由启动流程输出。
    """

    def test_flag_set_when_spaces_stripped(self, monkeypatch) -> None:
        cfg = _reload(monkeypatch, SMTP_PASSWORD="abcd efgh ijkl mnop")
        assert cfg.SMTP_PASSWORD_NORMALISED is True

    def test_flag_clear_when_already_clean(self, monkeypatch) -> None:
        cfg = _reload(monkeypatch, SMTP_PASSWORD="abcdefghijklmnop")
        assert cfg.SMTP_PASSWORD_NORMALISED is False

    def test_flag_clear_when_unset(self, monkeypatch) -> None:
        cfg = _reload(monkeypatch, SMTP_PASSWORD="")
        assert cfg.SMTP_PASSWORD_NORMALISED is False


class TestExternalMailer:
    """外部代发模式必须关掉进程内定时器。

    不关的话两个调度器会在周一 09:00 抢同一批论文：进程内那次因为
    Railway 封了 SMTP 必然失败，紧接着 GitHub Actions 又跑一次检查。
    """

    def test_defaults_off(self, monkeypatch) -> None:
        monkeypatch.delenv("EXTERNAL_MAILER", raising=False)
        cfg = _reload(monkeypatch)
        assert cfg.EXTERNAL_MAILER is False

    def test_enabled(self, monkeypatch) -> None:
        cfg = _reload(monkeypatch, EXTERNAL_MAILER="true")
        assert cfg.EXTERNAL_MAILER is True

    def test_scheduler_skips_weekly_job(self, monkeypatch) -> None:
        from app import scheduler

        cfg = _reload(monkeypatch, EXTERNAL_MAILER="true")
        monkeypatch.setattr(scheduler, "config", cfg)
        monkeypatch.setattr(scheduler, "_scheduler", None)
        sched = scheduler.start()
        try:
            assert sched.get_job("weekly-bis-check") is None
            assert "GitHub Actions" in scheduler.next_run_time()
        finally:
            sched.shutdown(wait=False)
            monkeypatch.setattr(scheduler, "_scheduler", None)
