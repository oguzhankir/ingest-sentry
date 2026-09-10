"""Strict transcoding, single-snapshot integrity and atomic publication checks."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import traceback
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from ingest_sentry import inspection, normalization
from ingest_sentry.contracts import ColumnRule, ImportContract
from ingest_sentry.errors import InspectionFailed
from ingest_sentry.normalization import normalize_file


@pytest.mark.parametrize("encoding", ["cp1254", "utf-8", "utf-16", "utf-32"])
def test_normalization_preserves_text_and_source_bytes(make_file, tmp_path, encoding):
    text = 'id;description\r\n001;"İstanbul; ışık\nsecond line"\r\n002;"say ""yes"""\r'
    source = make_file(text, encoding=encoding)
    original = source.read_bytes()
    output = tmp_path / "normalized.csv"

    result = normalize_file(source, output, encoding=encoding, delimiter=";")

    assert output.read_bytes() == text.encode("utf-8")
    assert source.read_bytes() == original
    assert result.report.ok
    assert result.report.sha256 == hashlib.sha256(original).hexdigest()
    assert result.report.records == 2
    assert result.output == output.name
    assert result.output_bytes == len(output.read_bytes())
    assert result.output_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["input.csv", "normalized.csv"]


def test_result_is_frozen_json_serializable_and_content_redacted(make_file, tmp_path):
    source = make_file("private_header\nTOP_SECRET_VALUE\n", name="private/export.csv")
    result = normalize_file(source, tmp_path / "out.csv", encoding="utf-8", delimiter=",")
    payload = json.dumps(result.to_dict())

    assert json.loads(payload) == {
        "ok": True,
        "output": "out.csv",
        "output_bytes": result.output_bytes,
        "output_sha256": result.output_sha256,
        "inspection": json.loads(json.dumps(result.report.to_dict())),
    }
    assert "private_header" not in payload
    assert "TOP_SECRET_VALUE" not in payload
    assert str(source) not in payload
    assert "private/" not in payload
    with pytest.raises(FrozenInstanceError):
        result.output = "changed.csv"


@pytest.mark.parametrize("encoding, keep_bom", [("utf-8", True), ("utf-8-sig", False)])
def test_bom_handling_follows_explicit_source_codec(make_file, tmp_path, encoding, keep_bom):
    content = b"\xef\xbb\xbfname\nAda\n"
    source = make_file(content)
    output = tmp_path / "out.csv"

    normalize_file(source, output, encoding=encoding, delimiter=",")

    assert output.read_bytes() == (content if keep_bom else content[3:])
    assert source.read_bytes() == content


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 65536])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32", "utf-7"])
def test_incremental_decoder_handles_split_characters(
    make_file, tmp_path, monkeypatch, chunk_size, encoding
):
    text = "name\nİstanbul £ 日 😀\n"
    source = make_file(text, encoding=encoding)
    output = tmp_path / "out.csv"
    monkeypatch.setattr(normalization, "_CHUNK", chunk_size)

    normalize_file(source, output, encoding=encoding, delimiter=",")

    assert output.read_bytes() == text.encode("utf-8")


def test_final_decoder_flush_is_written(make_file, tmp_path, monkeypatch):
    source = make_file(b"name\n+AKM")  # UTF-7 final shifted sequence without a terminator.
    output = tmp_path / "out.csv"
    monkeypatch.setattr(normalization, "_CHUNK", 1)

    normalize_file(source, output, encoding="utf-7", delimiter=",")

    assert output.read_bytes() == "name\n£".encode()


@pytest.mark.parametrize(
    "content,encoding",
    [
        (b"id,name\n1,\xff\n", "utf-8"),
        (b"id,name\n1\n", "utf-8"),
        (b'id,name\n1,"unterminated\n', "utf-8"),
        (b"id,name\n1,secret\x00\n", "utf-8"),
        (b"", "utf-8"),
    ],
)
def test_invalid_input_never_creates_output_or_temporary_file(
    make_file, tmp_path, content, encoding
):
    source = make_file(content)
    output = tmp_path / "out.csv"

    with pytest.raises(InspectionFailed) as caught:
        normalize_file(source, output, encoding=encoding, delimiter=",")

    assert not caught.value.report.ok
    assert source.read_bytes() == content
    assert list(tmp_path.iterdir()) == [source]


def ambiguous_detection(monkeypatch, source):
    monkeypatch.setattr(
        inspection.bytesense,
        "from_fp",
        lambda _: SimpleNamespace(
            encoding="utf-8",
            status="ambiguous",
            confidence=0.5,
            bytes_validated=source.stat().st_size,
            complete=True,
        ),
    )


def test_ambiguous_encoding_requires_opt_in(make_file, tmp_path, monkeypatch):
    source = make_file("id,name\n1,Ada\n")
    ambiguous_detection(monkeypatch, source)
    output = tmp_path / "out.csv"

    with pytest.raises(InspectionFailed, match="Ambiguous encoding") as caught:
        normalize_file(source, output, delimiter=",")

    assert caught.value.report.ok
    assert caught.value.report.encoding.status == "ambiguous"
    assert not output.exists()
    assert list(tmp_path.iterdir()) == [source]


def test_ambiguous_encoding_explicit_opt_in(make_file, tmp_path, monkeypatch):
    source = make_file("id,name\n1,Ada\n")
    ambiguous_detection(monkeypatch, source)
    output = tmp_path / "out.csv"

    result = normalize_file(source, output, delimiter=",", accept_ambiguous=True)

    assert result.report.encoding.status == "ambiguous"
    assert output.read_bytes() == source.read_bytes()


def test_explicit_encoding_bypasses_ambiguous_detection(make_file, tmp_path, monkeypatch):
    source = make_file("id,name\n1,Ada\n")

    def unexpected_detection(_):
        pytest.fail("An explicitly provided encoding must not invoke detection.")

    monkeypatch.setattr(inspection.bytesense, "from_fp", unexpected_detection)
    result = normalize_file(source, tmp_path / "out.csv", encoding="utf-8", delimiter=",")

    assert result.report.encoding.origin == "provided"


def test_contract_supplies_codec_delimiter_and_header(make_file, tmp_path):
    source = make_file("001;İstanbul\r\n002;Ankara\r\n", encoding="cp1254")
    contract = ImportContract(
        columns=(ColumnRule("id", "integer"), ColumnRule("city")),
        encoding="cp1254",
        delimiter=";",
        header=False,
        min_records=2,
    )
    output = tmp_path / "out.csv"

    result = normalize_file(source, output, contract=contract)

    assert output.read_bytes() == source.read_bytes().decode("cp1254").encode("utf-8")
    assert result.report.records == 2
    assert not result.report.dialect.header


def test_contract_failure_prevents_output(make_file, tmp_path):
    source = make_file("id\nnot-an-integer\n")
    contract = ImportContract(columns=(ColumnRule("id", "integer"),), encoding="utf-8")
    output = tmp_path / "out.csv"

    with pytest.raises(InspectionFailed) as caught:
        normalize_file(source, output, contract=contract)

    assert "contract_type" in {issue.code for issue in caught.value.report.issues}
    assert list(tmp_path.iterdir()) == [source]


def test_conversion_uses_captured_bytes_even_when_source_changes(make_file, tmp_path, monkeypatch):
    original = "name\nİstanbul\n".encode("cp1254")
    source = make_file(original)
    real_session = normalization._inspection_session

    @contextmanager
    def changed_source_session(*args, **kwargs):
        with real_session(*args, **kwargs) as captured:
            source.write_bytes(b"BROKEN,new,file\n")
            yield captured

    monkeypatch.setattr(normalization, "_inspection_session", changed_source_session)
    output = tmp_path / "out.csv"
    result = normalize_file(source, output, encoding="cp1254", delimiter=",")

    assert output.read_bytes() == original.decode("cp1254").encode("utf-8")
    assert result.report.sha256 == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == b"BROKEN,new,file\n"


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "100", None])
def test_invalid_output_limits_are_rejected_before_io(tmp_path, value):
    with pytest.raises(ValueError, match="max_output_bytes"):
        normalize_file(tmp_path / "missing.csv", tmp_path / "out.csv", max_output_bytes=value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_invalid_ambiguity_policy_is_rejected_before_io(tmp_path, value):
    with pytest.raises(ValueError, match="accept_ambiguous"):
        normalize_file(tmp_path / "missing.csv", tmp_path / "out.csv", accept_ambiguous=value)
    assert not list(tmp_path.iterdir())


def test_input_limit_prevents_output(make_file, tmp_path):
    source = make_file("name\nAda\n")
    with pytest.raises(InspectionFailed) as caught:
        normalize_file(source, tmp_path / "out.csv", encoding="utf-8", max_bytes=3)
    assert caught.value.report.issues[0].code == "file_too_large"
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_output_limit_applies_to_utf8_bytes_not_input_bytes(make_file, tmp_path, delta):
    text = "name\nİİİİ\n"
    source = make_file(text, encoding="cp1254")
    output = tmp_path / "out.csv"
    limit = len(text.encode("utf-8")) + delta

    if delta < 0:
        with pytest.raises(InspectionFailed, match="max_output_bytes") as caught:
            normalize_file(source, output, encoding="cp1254", max_output_bytes=limit)
        assert caught.value.report.ok
        assert list(tmp_path.iterdir()) == [source]
    else:
        result = normalize_file(source, output, encoding="cp1254", max_output_bytes=limit)
        assert result.output_bytes == len(text.encode("utf-8"))
        assert output.read_bytes() == text.encode("utf-8")
    assert source.read_bytes() == text.encode("cp1254")


def test_limit_after_several_chunks_cleans_partial_temporary_file(make_file, tmp_path, monkeypatch):
    source = make_file("name\nİİİİ\n", encoding="cp1254")
    monkeypatch.setattr(normalization, "_CHUNK", 2)

    with pytest.raises(InspectionFailed, match="max_output_bytes"):
        normalize_file(source, tmp_path / "out.csv", encoding="cp1254", max_output_bytes=8)

    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("destination_kind", ["source", "file", "directory", "hardlink"])
def test_existing_destinations_are_never_overwritten(make_file, tmp_path, destination_kind):
    source = make_file("name\nAda\n")
    output = tmp_path / "out.csv"
    if destination_kind == "source":
        output = source
    elif destination_kind == "file":
        output.write_bytes(b"keep private output")
    elif destination_kind == "directory":
        output.mkdir()
    else:
        os.link(source, output)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    with pytest.raises(FileExistsError):
        normalize_file(source, output, encoding="utf-8")

    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert not list(tmp_path.glob(".ingest-sentry-*.tmp"))


def symlink_or_skip(target, link):
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symbolic link creation is not permitted on this platform.")


@pytest.mark.parametrize("dangling", [False, True])
def test_symlink_destinations_are_never_followed(make_file, tmp_path, dangling):
    source = make_file("name\nAda\n")
    target = tmp_path / "target.csv"
    if not dangling:
        target.write_bytes(b"keep target")
    output = tmp_path / "out.csv"
    symlink_or_skip(target, output)

    with pytest.raises(FileExistsError):
        normalize_file(source, output, encoding="utf-8")

    assert output.is_symlink()
    assert target.exists() is not dangling
    if not dangling:
        assert target.read_bytes() == b"keep target"
    assert source.read_bytes() == b"name\nAda\n"


@pytest.mark.parametrize("racing_kind", ["file", "symlink"])
def test_publication_race_never_overwrites_other_writer(
    make_file, tmp_path, monkeypatch, racing_kind
):
    source = make_file("name\nAda\n")
    output = tmp_path / "out.csv"
    target = tmp_path / "unrelated.csv"
    target.write_bytes(b"keep unrelated")
    real_link = os.link

    def raced_link(temporary, destination, *, follow_symlinks):
        assert not follow_symlinks
        if racing_kind == "file":
            output.write_bytes(b"racing writer")
        else:
            symlink_or_skip(target, output)
        return real_link(temporary, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(normalization.os, "link", raced_link)
    with pytest.raises(FileExistsError):
        normalize_file(source, output, encoding="utf-8")

    assert output.read_bytes() == (b"racing writer" if racing_kind == "file" else b"keep unrelated")
    assert target.read_bytes() == b"keep unrelated"
    assert source.read_bytes() == b"name\nAda\n"
    assert not list(tmp_path.glob(".ingest-sentry-*.tmp"))


@pytest.mark.parametrize("failure", ["create", "write", "flush", "fsync", "link"])
def test_io_failures_leave_no_output_and_clean_owned_temporary_file(
    make_file, tmp_path, monkeypatch, failure
):
    source = make_file("name\nAda\n")
    output = tmp_path / "out.csv"

    def fail(*args, **kwargs):
        raise OSError("simulated IO failure")

    if failure in {"create", "write", "flush"}:
        real_create = normalization.NamedTemporaryFile

        def create(*args, **kwargs):
            if failure == "create":
                return fail()
            target = real_create(*args, **kwargs)
            setattr(target, failure, fail)
            return target

        monkeypatch.setattr(normalization, "NamedTemporaryFile", create)
    else:
        monkeypatch.setattr(normalization.os, failure, fail)

    with pytest.raises(OSError, match="simulated IO failure"):
        normalize_file(source, output, encoding="utf-8")

    assert list(tmp_path.iterdir()) == [source]
    assert source.read_bytes() == b"name\nAda\n"


def test_unsupported_hardlinks_fail_without_fallback(make_file, tmp_path, monkeypatch):
    source = make_file("name\nAda\n")

    def unsupported(*args, **kwargs):
        raise NotImplementedError("No hard links on this filesystem")

    monkeypatch.setattr(normalization.os, "link", unsupported)
    with pytest.raises(OSError, match="atomic output publication"):
        normalize_file(source, tmp_path / "out.csv", encoding="utf-8")

    assert list(tmp_path.iterdir()) == [source]


def test_missing_destination_parent_is_not_created(make_file, tmp_path):
    source = make_file("name\nAda\n")
    with pytest.raises(FileNotFoundError):
        normalize_file(source, tmp_path / "missing" / "out.csv", encoding="utf-8")
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.skipif(os.name == "nt", reason="Windows permissions use ACLs, not POSIX mode bits.")
def test_temporary_and_output_permissions_are_private(make_file, tmp_path, monkeypatch):
    source = make_file("name\nAda\n")
    output = tmp_path / "out.csv"
    real_link = os.link

    def check_private_then_link(temporary, destination, *, follow_symlinks):
        assert stat.S_IMODE(Path(temporary).stat().st_mode) == 0o600
        assert Path(temporary).read_bytes() == source.read_bytes()
        assert not Path(destination).exists()
        return real_link(temporary, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(normalization.os, "link", check_private_then_link)
    normalize_file(source, output, encoding="utf-8")

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".ingest-sentry-*.tmp"))


def test_unicode_transcoding_failure_is_redacted_and_cleans_temporary_file(
    make_file, tmp_path, monkeypatch
):
    source = make_file("name\nPRIVATE_VALUE\n")
    real_session = normalization._inspection_session

    @contextmanager
    def corrupted_snapshot_session(*args, **kwargs):
        with real_session(*args, **kwargs) as (report, snapshot):
            snapshot.seek(0)
            snapshot.truncate()
            snapshot.write(b"PRIVATE_VALUE\xff")
            snapshot.seek(0)
            yield report, snapshot

    monkeypatch.setattr(normalization, "_inspection_session", corrupted_snapshot_session)
    with pytest.raises(InspectionFailed, match="strictly transcoded") as caught:
        normalize_file(source, tmp_path / "out.csv", encoding="utf-8")

    assert "PRIVATE_VALUE" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert "UnicodeDecodeError" not in "".join(traceback.format_exception(caught.value))
    assert list(tmp_path.iterdir()) == [source]
    assert source.read_bytes() == b"name\nPRIVATE_VALUE\n"


def test_short_write_is_not_reported_as_success(make_file):
    source = make_file("name\nAda\n")
    report = inspection.inspect_file(source, encoding="utf-8")

    class ShortWriter(io.BytesIO):
        def write(self, value):
            return super().write(value[:1])

    with pytest.raises(OSError, match="written completely"):
        normalization._transcode(io.BytesIO(source.read_bytes()), ShortWriter(), report, 100)


def test_base_exception_still_cleans_temporary_output(make_file, tmp_path, monkeypatch):
    source = make_file("name\nAda\n")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(normalization, "_transcode", interrupted)
    with pytest.raises(KeyboardInterrupt):
        normalize_file(source, tmp_path / "out.csv", encoding="utf-8")

    assert list(tmp_path.iterdir()) == [source]


def test_cleanup_failure_reports_io_error_but_published_output_is_complete(
    make_file, tmp_path, monkeypatch
):
    source = make_file("name\nİstanbul\n", encoding="cp1254")
    original = source.read_bytes()
    output = tmp_path / "out.csv"
    real_unlink = Path.unlink
    failed_paths = []

    def fail_temporary_unlink(path, **kwargs):
        if path.name.startswith(".ingest-sentry-"):
            failed_paths.append(path)
            raise PermissionError("simulated cleanup failure")
        return real_unlink(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)
    try:
        with pytest.raises(PermissionError, match="cleanup failure"):
            normalize_file(source, output, encoding="cp1254")
        assert output.read_bytes() == original.decode("cp1254").encode("utf-8")
        assert source.read_bytes() == original
        assert len(failed_paths) == 1
        assert failed_paths[0].read_bytes() == output.read_bytes()
    finally:
        for temporary in failed_paths:
            real_unlink(temporary)


def test_utf7_surrogate_encode_failure_never_leaks_input(make_file, tmp_path):
    source = make_file(b"name\nPRIVATE_VALUE+2AA-\n")
    with pytest.raises(InspectionFailed, match="strictly transcoded") as caught:
        normalize_file(source, tmp_path / "out.csv", encoding="utf-7")

    assert caught.value.report.ok
    assert "PRIVATE_VALUE" not in str(caught.value)
    assert "UnicodeEncodeError" not in "".join(traceback.format_exception(caught.value))
    assert list(tmp_path.iterdir()) == [source]


def test_detected_bom_encoding_is_transcoded_without_manual_override(make_file, tmp_path):
    text = "id,name\n001,İstanbul\n"
    source = make_file(text, encoding="utf-16")
    output = tmp_path / "out.csv"

    result = normalize_file(source, output)

    assert result.report.encoding.origin == "detected"
    assert output.read_bytes() == text.encode("utf-8")


def test_output_is_flushed_and_fsynced_before_it_is_published(make_file, tmp_path, monkeypatch):
    source = make_file("name\nAda\n")
    output = tmp_path / "out.csv"
    real_link = os.link
    real_fsync = os.fsync
    fsynced = []

    def fsync_before_link(descriptor):
        assert not output.exists()
        fsynced.append(True)
        return real_fsync(descriptor)

    def check_fsync_then_link(temporary, destination, *, follow_symlinks):
        assert fsynced == [True]
        assert Path(temporary).read_bytes() == source.read_bytes()
        return real_link(temporary, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(normalization.os, "fsync", fsync_before_link)
    monkeypatch.setattr(normalization.os, "link", check_fsync_then_link)

    normalize_file(source, output, encoding="utf-8")

    assert output.read_bytes() == source.read_bytes()
