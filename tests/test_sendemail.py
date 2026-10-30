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
    sendemail.send_email("Subj", "<b>Body</b>", "Body text")
    out = capsys.readouterr().out
    assert "[DRY RUN] Email not sent" in out
    assert "Subject: Subj" in out
    assert "<b>Body</b>" in out


def test_c18_dry_run_without_any_email_env_prints_not_configured(monkeypatch, capsys):
    """RC-15 criterion 33: exact new dry-run block, returns True."""
    for name in ("SENDER_EMAIL", "SENDER_PASSWORD", "RECEIVER_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")
    _no_smtp(monkeypatch)
    result = sendemail.send_email("Subj", "<b>H</b>", "T")
    assert result is True
    assert capsys.readouterr().out == (
        "[DRY RUN] Email not sent (RAINCHECK_DRY_RUN is set).\n"
        "To: (not configured)\n"
        "Subject: Subj\n"
        "--- text/plain ---\n"
        "T\n"
        "--- text/html ---\n"
        "<b>H</b>\n"
    )


def test_c19_dry_run_recipient_prefers_receiver_then_sender(monkeypatch, capsys):
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")
    _no_smtp(monkeypatch)
    monkeypatch.setenv("SENDER_EMAIL", "a@example.com")
    sendemail.send_email("S", "B", "T")
    assert "To: a@example.com" in capsys.readouterr().out

    monkeypatch.setenv("RECEIVER_EMAIL", "b@example.com")
    sendemail.send_email("S", "B", "T")
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
    sendemail.send_email("Subj", "Body", "Body text")
    assert record["init"] == [("smtp.gmail.com", 587)]
    assert record["starttls"] == 1
    assert record["login"] == [("a@example.com", "pw")]
    assert len(record["sendmail"]) == 1
    sender, receiver, message = record["sendmail"][0]
    assert (sender, receiver) == ("a@example.com", "b@example.com")
    assert "Subject: Subj" in message


def test_rc03b_c47_smtp_connection_has_a_30_second_timeout(monkeypatch):
    monkeypatch.delenv("RAINCHECK_DRY_RUN", raising=False)
    monkeypatch.setenv("SENDER_EMAIL", "a@example.com")
    monkeypatch.setenv("SENDER_PASSWORD", "pw")
    monkeypatch.setenv("RECEIVER_EMAIL", "b@example.com")
    inits = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            inits.append((args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def sendmail(self, sender, receiver, message):
            pass

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    sendemail.send_email("Subj", "Body", "Body text")
    assert inits == [(("smtp.gmail.com", 587), {"timeout": 30})]


# ---- RC-15 criteria 31-32: multipart/alternative and the True/False result ---------


def _recording_smtp(monkeypatch, *, fail_in=None):
    """Installs a fake smtplib.SMTP; fail_in is None, 'init', 'login' or 'sendmail'."""
    monkeypatch.delenv("RAINCHECK_DRY_RUN", raising=False)
    monkeypatch.setenv("SENDER_EMAIL", "a@example.com")
    monkeypatch.setenv("SENDER_PASSWORD", "pw")
    monkeypatch.setenv("RECEIVER_EMAIL", "b@example.com")
    sent = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            if fail_in == "init":
                raise OSError("down")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            if fail_in == "login":
                raise smtplib.SMTPException("boom")

        def sendmail(self, sender, receiver, message):
            if fail_in == "sendmail":
                raise smtplib.SMTPException("boom")
            sent.append(message)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return sent


def test_rc15_c31_real_path_sends_multipart_alternative_text_then_html(monkeypatch):
    import email

    sent = _recording_smtp(monkeypatch)
    assert sendemail.send_email("Subj", "<p>テスト</p>", "テスト") is True
    assert len(sent) == 1
    msg = email.message_from_string(sent[0])
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["Subject"] == "Subj"
    parts = msg.get_payload()
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert [p.get_content_charset() for p in parts] == ["utf-8", "utf-8"]
    assert parts[0].get_payload(decode=True).decode("utf-8") == "テスト"
    assert parts[1].get_payload(decode=True).decode("utf-8") == "<p>テスト</p>"


@pytest.mark.parametrize("fail_in", ["init", "login", "sendmail"])
def test_rc15_c32_smtp_failure_returns_false_and_logs_error(
    monkeypatch, caplog, fail_in
):
    import logging

    _recording_smtp(monkeypatch, fail_in=fail_in)
    with caplog.at_level(logging.ERROR, logger="sendemail"):
        result = sendemail.send_email("Subj", "<p>x</p>", "x")
    assert result is False
    assert any(record.levelname == "ERROR" for record in caplog.records)
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "password" not in rendered and "<p>x</p>" not in rendered


def test_rc15_c34_missing_credential_still_raises_keyerror(monkeypatch):
    monkeypatch.delenv("RAINCHECK_DRY_RUN", raising=False)
    monkeypatch.delenv("SENDER_EMAIL", raising=False)
    monkeypatch.delenv("SENDER_PASSWORD", raising=False)
    with pytest.raises(KeyError):
        sendemail.send_email("Subj", "<p>x</p>", "x")


def test_rc10_c25_real_smtp_logs_outcome_without_credentials_or_message(
    monkeypatch, caplog
):
    import logging

    monkeypatch.delenv("RAINCHECK_DRY_RUN", raising=False)
    monkeypatch.setenv("SENDER_EMAIL", "sender-private@example.com")
    monkeypatch.setenv("SENDER_PASSWORD", "password-private")
    monkeypatch.setenv("RECEIVER_EMAIL", "receiver-private@example.com")

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def starttls(self):
            pass
        def login(self, *args):
            pass
        def sendmail(self, *args):
            return {}

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    with caplog.at_level(logging.INFO, logger="sendemail"):
        assert sendemail.send_email("private subject", "private html", "private text") is True
    assert any(record.levelname == "INFO" for record in caplog.records)
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    for private in (
        "sender-private@example.com", "receiver-private@example.com",
        "password-private", "private subject", "private html", "private text",
    ):
        assert private not in rendered


# ---- RC-19 inline image MIME --------------------------------------------------------


def test_rc19_c1_c2_sends_related_html_and_matching_inline_photo(monkeypatch):
    from email import policy
    from email.parser import Parser
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class PreparedImage:
        cid: str
        content_type: str
        data: bytes

    photo_bytes = b"normalized jpeg thumbnail bytes"
    photo = PreparedImage("opaque-photo-id", "image/jpeg", photo_bytes)
    sent = _recording_smtp(monkeypatch)

    assert sendemail.send_email(
        "Subj", '<img src="cid:opaque-photo-id">', "plain alternative",
        inline_images=(photo,),
    ) is True
    assert len(sent) == 1
    msg = Parser(policy=policy.default).parsestr(sent[0])
    assert msg.get_content_type() == "multipart/alternative"
    alternatives = list(msg.iter_parts())
    assert [part.get_content_type() for part in alternatives] == [
        "text/plain", "multipart/related",
    ]
    assert alternatives[0].get_content() == "plain alternative"

    related = alternatives[1]
    related_parts = list(related.iter_parts())
    assert [part.get_content_type() for part in related_parts] == [
        "text/html", "image/jpeg",
    ]
    assert related_parts[0].get_content() == '<img src="cid:opaque-photo-id">'
    image_part = related_parts[1]
    assert image_part["Content-ID"] == "<opaque-photo-id>"
    assert image_part["Content-Disposition"].startswith("inline")
    assert image_part.get_payload(decode=True) == photo_bytes
    assert "opaque-photo-id" not in msg["From"]
    assert "opaque-photo-id" not in msg["To"]


def test_rc19_c2_cid_is_referenced_once_and_has_no_orphan_image_parts(monkeypatch):
    from email import policy
    from email.parser import Parser
    from dataclasses import dataclass
    import re

    @dataclass(frozen=True)
    class PreparedImage:
        cid: str
        content_type: str
        data: bytes

    images = (
        PreparedImage("first-cid", "image/jpeg", b"one"),
        PreparedImage("second-cid", "image/png", b"two"),
    )
    sent = _recording_smtp(monkeypatch)
    html = '<img src="cid:first-cid"><img src="cid:second-cid">'
    sendemail.send_email("S", html, "T", inline_images=images)
    msg = Parser(policy=policy.default).parsestr(sent[0])
    related = list(msg.iter_parts())[1]
    parts = list(related.iter_parts())
    html_body = parts[0].get_content()
    referenced = re.findall(r'src="cid:([^"]+)"', html_body)
    attached = [part["Content-ID"].strip("<>") for part in parts[1:]]
    assert referenced == ["first-cid", "second-cid"]
    assert attached == referenced


def test_rc19_c21_dry_run_prints_markup_without_image_bytes_or_base64(monkeypatch, capsys):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class PreparedImage:
        cid: str
        content_type: str
        data: bytes

    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")
    _no_smtp(monkeypatch)
    image = PreparedImage("safe-cid", "image/jpeg", b"DO-NOT-PRINT-IMAGE-BYTES")
    assert sendemail.send_email(
        "Subj", '<img src="cid:safe-cid">', "text", inline_images=(image,),
    ) is True
    output = capsys.readouterr().out
    assert '<img src="cid:safe-cid">' in output
    assert "DO-NOT-PRINT-IMAGE-BYTES" not in output
    assert "RE8tTk9ULVBSSU5ULUlNQUdFLUJZVEVT" not in output
