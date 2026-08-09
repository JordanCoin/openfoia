"""Security regression tests: remaining hardening items."""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Alembic must use the SQLCipher connection it is handed
# ---------------------------------------------------------------------------


def test_alembic_env_uses_the_passed_connection():
    """Ignoring config.attributes silently skipped migrating encrypted DBs."""
    from pathlib import Path

    import openfoia

    env = (Path(openfoia.__file__).parent / "migrations" / "env.py").read_text()

    assert 'config.attributes.get("connection"' in env, (
        "env.py does not read the connection passed by db.run_migrations()"
    )


def test_db_upgrade_command_does_not_build_a_keyless_url():
    """`openfoia db upgrade` hardcoded sqlite:///<path> with no key."""
    import inspect

    from openfoia import cli

    src = inspect.getsource(cli.upgrade)

    assert 'f"sqlite:///{db_path}"' not in src
    assert "run_migrations" in src


# ---------------------------------------------------------------------------
# Printed letters must escape interpolated fields
# ---------------------------------------------------------------------------

MARKUP = 'Evil <b>Corp</b> & "Sons" <script>x</script>'


def test_mail_letter_escapes_recipient_name():
    from openfoia.gateways.mail import _esc

    out = _esc(MARKUP)

    assert "<b>" not in out
    assert "<script>" not in out
    assert "&lt;b&gt;" in out
    assert "&amp;" in out


def test_fax_cover_escapes_recipient_name():
    from openfoia.gateways.fax import _esc

    out = _esc(MARKUP)

    assert "<b>" not in out
    assert "&lt;b&gt;" in out


def test_escape_helpers_handle_none():
    from openfoia.gateways.fax import _esc as fax_esc
    from openfoia.gateways.mail import _esc as mail_esc

    assert fax_esc(None) == ""
    assert mail_esc(None) == ""


def test_mail_template_has_no_unescaped_field_interpolation():
    """Every {payload.*} in the letter template must go through _esc()."""
    import inspect
    import re

    from openfoia.gateways import mail

    src = inspect.getsource(mail)
    # Bare {payload.field} inside the HTML template (not wrapped in _esc()).
    bare = re.findall(r"(?<!_esc\()\{payload\.(recipient_name|subject)\}", src)

    assert bare == [], f"unescaped fields in letter template: {bare}"


# ---------------------------------------------------------------------------
# Honesty: don't claim protections we don't deliver
# ---------------------------------------------------------------------------


def test_tor_browse_does_not_claim_webrtc_is_disabled():
    """`--disable-webrtc` is not a real Chromium flag; don't promise it."""
    from openfoia.tor_browse import _TOR_WARNING, browse  # noqa: F401
    import inspect

    from openfoia import tor_browse

    assert "WebRTC disabled (prevents IP leaks)" not in _TOR_WARNING

    # Ignore comments: the point is that the flag is not actually passed.
    code = "\n".join(
        line
        for line in inspect.getsource(tor_browse).splitlines()
        if not line.strip().startswith("#")
    )
    assert "--disable-webrtc" not in code, "non-existent Chromium flag still passed"


def test_tor_browse_warning_points_at_tor_browser_for_real_anonymity():
    from openfoia.tor_browse import _TOR_WARNING

    lowered = _TOR_WARNING.lower()
    assert "not" in lowered
    assert "tor browser" in lowered or "tails" in lowered


def test_install_script_fails_closed_without_checksum_tooling():
    from pathlib import Path

    import openfoia

    script = (Path(openfoia.__file__).parent.parent / "install.sh").read_text()

    # The old code silently set actual=expected to skip comparison.
    assert "--insecure-skip-verify" in script, "no explicit opt-out flag"
    assert "SKIP_VERIFY" in script


def test_install_script_documents_working_portable_invocation():
    """`| bash --portable` is eaten by bash and silently installs to the host."""
    from pathlib import Path

    import openfoia

    script = (Path(openfoia.__file__).parent.parent / "install.sh").read_text()

    assert "bash -s -- --portable" in script
    assert "| bash --portable" not in script


def test_uninstall_script_mentions_secure_purge():
    from pathlib import Path

    import openfoia

    script = (Path(openfoia.__file__).parent.parent / "uninstall.sh").read_text()

    assert "purge --secure" in script
