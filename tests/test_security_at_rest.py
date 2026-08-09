"""Security regression tests: what stays on disk.

The encryption and duress features are the ones a journalist relies on when
a device is seized. These tests pin the properties they claim to provide.
"""

from __future__ import annotations

import os
import stat

import pytest


# ---------------------------------------------------------------------------
# `db encrypt` must not leave the plaintext database recoverable
# ---------------------------------------------------------------------------


def test_secure_delete_plaintext_db_removes_sidecars(tmp_path):
    """WAL/SHM/journal hold recent plaintext writes and must go too."""
    from openfoia.db import secure_delete_plaintext_db

    db = tmp_path / "data.db"
    db.write_bytes(b"PLAINTEXT-DATABASE-CONTENT" * 100)
    sidecars = [
        tmp_path / "data.db-wal",
        tmp_path / "data.db-shm",
        tmp_path / "data.db-journal",
    ]
    for s in sidecars:
        s.write_bytes(b"PLAINTEXT-WAL-CONTENT" * 50)

    secure_delete_plaintext_db(db)

    assert not db.exists()
    for s in sidecars:
        assert not s.exists(), f"{s.name} left on disk"


def test_secure_delete_plaintext_db_overwrites_content(tmp_path):
    """The file's own blocks must be overwritten, not just unlinked."""
    from openfoia.db import secure_delete_plaintext_db

    db = tmp_path / "data.db"
    secret = b"TOP-SECRET-INVESTIGATION-DATA"
    db.write_bytes(secret * 100)

    observed = {}
    real_open = open

    def spy_open(path, mode="r", *a, **k):
        if str(path) == str(db) and "b" in mode and ("+" in mode or "w" in mode):
            observed["overwritten"] = True
        return real_open(path, mode, *a, **k)

    import builtins

    builtins.open = spy_open
    try:
        secure_delete_plaintext_db(db)
    finally:
        builtins.open = real_open

    assert observed.get("overwritten"), "plaintext DB was unlinked without overwriting"
    assert not db.exists()


def test_secure_delete_plaintext_db_tolerates_missing_file(tmp_path):
    from openfoia.db import secure_delete_plaintext_db

    secure_delete_plaintext_db(tmp_path / "nope.db")  # must not raise


def test_encrypt_database_does_not_use_rename_backup_dance():
    """Regression: copy2->move left the ORIGINAL plaintext blocks unfreed.

    The original was renamed away (never overwritten) and only the *copy*
    was secure-deleted, so file carving recovered the whole pre-encryption
    database. Encryption must shred the original in place.
    """
    import inspect

    from openfoia.db import encrypt_database

    src = inspect.getsource(encrypt_database)

    assert ".db.bak" not in src, "plaintext .bak copy is still created"
    assert "secure_delete_plaintext_db" in src, "original plaintext is not shredded in place"


# ---------------------------------------------------------------------------
# Duress mode — the filenames must actually be opaque and symmetric
# ---------------------------------------------------------------------------


def test_real_db_uses_a_profile_slot_not_data_db(tmp_path, monkeypatch):
    """`profile_1.db` next to `data.db` tells an examiner which is the decoy.

    The documented design is two symmetric opaque slots. Once duress mode is
    configured the real database must live in a slot too, so the on-disk
    layout does not label the decoy.
    """
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    from openfoia.security import get_profile_paths, real_profile_path

    slots = get_profile_paths()
    assert real_profile_path() in slots
    assert real_profile_path().name != "data.db"


def test_profile_slot_names_are_indistinguishable():
    """Neither slot may be named in a way that reveals its role."""
    from openfoia.security import _PROFILE_SLOTS

    for name in _PROFILE_SLOTS:
        lowered = name.lower()
        for tell in ("decoy", "duress", "real", "fake", "data"):
            assert tell not in lowered, f"slot name {name!r} leaks its role"


def test_setup_duress_mode_requires_encryption(tmp_path, monkeypatch):
    """A plaintext decoy contradicts the guarantee — fail closed instead."""
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    import openfoia.security as sec

    if sec._has_sqlcipher():
        pytest.skip("pysqlcipher3 installed; the fail-closed path cannot trigger")

    with pytest.raises(RuntimeError, match="encryption"):
        sec.setup_duress_mode("duress-pass")


def test_duress_docs_do_not_claim_unimplemented_opacity():
    """Honesty check: no claim we cannot back up."""
    from pathlib import Path

    import openfoia

    threat_model = Path(openfoia.__file__).parent.parent / "docs" / "THREAT_MODEL.md"
    text = threat_model.read_text()

    # If we claim opaque filenames, both slots must really be opaque.
    if "Opaque filenames" in text:
        from openfoia.security import _PROFILE_SLOTS

        assert "data.db" not in text.split("Opaque filenames")[1][:200], (
            "THREAT_MODEL claims opaque filenames while documenting data.db"
        )
        assert len(_PROFILE_SLOTS) == 2


# ---------------------------------------------------------------------------
# File permissions — config and DB hold the crown jewels
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_data_dir_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "d"))
    from openfoia.db import get_data_dir

    d = get_data_dir()
    mode = stat.S_IMODE(d.stat().st_mode)

    assert mode & stat.S_IRWXG == 0, f"group bits set: {oct(mode)}"
    assert mode & stat.S_IRWXO == 0, f"other bits set: {oct(mode)}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_existing_data_dir_is_tightened(tmp_path, monkeypatch):
    """A dir created before this fix (0755) must be corrected on next use."""
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(d))
    from openfoia.db import get_data_dir

    got = get_data_dir()
    mode = stat.S_IMODE(got.stat().st_mode)

    assert mode & stat.S_IRWXO == 0, f"world bits still set: {oct(mode)}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_database_file_is_owner_only(tmp_path, monkeypatch):
    """Defence in depth: the 0700 dir protects it, but set the file too.

    If the data dir is ever placed on a volume with looser semantics, or its
    mode is changed, a 0644 database is readable by every local account.
    """
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    from openfoia.db import init_db

    init_db(seed=False)

    db = tmp_path / "data.db"
    assert db.exists()
    mode = stat.S_IMODE(db.stat().st_mode)
    assert mode & stat.S_IRWXG == 0, f"group bits set: {oct(mode)}"
    assert mode & stat.S_IRWXO == 0, f"other bits set: {oct(mode)}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_saved_config_is_not_world_readable(tmp_path, monkeypatch):
    """config.json can hold SMTP/Twilio/Lob secrets and the DB password."""
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    from openfoia.config import OpenFOIAConfig, save_config

    path = tmp_path / "config.json"
    save_config(OpenFOIAConfig(), path)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & stat.S_IRWXG == 0, f"group bits set: {oct(mode)}"
    assert mode & stat.S_IRWXO == 0, f"other bits set: {oct(mode)}"
