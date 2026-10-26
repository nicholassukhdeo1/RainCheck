import smtplib

import pytest

import sendemail


def _no_smtp(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("smtplib.SMTP must not be constructed in dry run")

    monkeypatch.setattr(smtplib, "SMTP", boom)


def test_c17_dry_run_prints_email_and_never_builds_smtp(monkeypatch, capsys):
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")
    _no_smtp(monkeypatch)
    sendemail.send_email("Subj", "<b>Body</b>")
    out = capsys.readouterr().out
    assert "[DRY RUN] Email not sent" in out
    assert "Subject: Subj" in out
    assert "<b>Body</b>" in out


def test_c18_dry_run_without_any_email_env_prints_not_configured(monkeypatch, capsys):
    for name in ("SENDER_EMAIL", "SENDER_PASSWORD", "RECEIVER_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")
    _no_smtp(monkeypatch)
    sendemail.send_email("Subj", "<b>Body</b>")
    assert capsys.readouterr().out == (
        "[DRY RUN] Email not sent (RAINCHECK_DRY_RUN is set).\n"
        "To: (not configured)\n"
        "Subject: Subj\n"
        "<b>Body</b>\n"
    )


def test_c19_dry_run_recipient_prefers_receiver_then_sender(monkeypatch, capsys):
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")
    _no_smtp(monkeypatch)
    monkeypatch.setenv("SENDER_EMAIL", "a@example.com")
    sendemail.send_email("S", "B")
    assert "To: a@example.com" in capsys.readouterr().out

    monkeypatch.setenv("RECEIVER_EMAIL", "b@example.com")
    sendemail.send_email("S", "B")
    assert "To: b@example.com" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "ture"])
def test_c20_is_dry_run_true_values(monkeypatch, value):
    monkeypatch.setenv("RAINCHECK_DRY_RUN", value)
    assert sendemail.is_dry_run() is True


@pytest.mark.parametrize("value", [None, "", "0", "false", "False", "no", "off"])
def test_c20_is_dry_run_false_values(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("RAINCHECK_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("RAINCHECK_DRY_RUN", value)
    assert sendemail.is_dry_run() is False


def test_c21_real_path_uses_gmail_smtp_with_login_and_sendmail(monkeypatch):
    monkeypatch.delenv("RAINCHECK_DRY_RUN", raising=False)
    monkeypatch.setenv("SENDER_EMAIL", "a@example.com")
    monkeypatch.setenv("SENDER_PASSWORD", "pw")
    monkeypatch.setenv("RECEIVER_EMAIL", "b@example.com")
    record = {"init": [], "starttls": 0, "login": [], "sendmail": []}

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            record["init"].append(args)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            record["starttls"] += 1

        def login(self, user, password):
            record["login"].append((user, password))

        def sendmail(self, sender, receiver, message):
            record["sendmail"].append((sender, receiver, message))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    sendemail.send_email("Subj", "Body")
    assert record["init"] == [("smtp.gmail.com", 587)]
    assert record["starttls"] == 1
    assert record["login"] == [("a@example.com", "pw")]
    assert len(record["sendmail"]) == 1
    sender, receiver, message = record["sendmail"][0]
    assert (sender, receiver) == ("a@example.com", "b@example.com")
    assert "Subject: Subj" in message
