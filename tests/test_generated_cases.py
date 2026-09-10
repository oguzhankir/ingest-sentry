"""Deterministic generated inputs exercise complete CSV records, not just prefixes."""

import csv
import io
import os
import random

import pytest

from ingest_sentry import inspect_file


def test_generated_quoted_records_round_trip_without_modification(tmp_path):
    randomizer = random.Random(20260910)
    vocabulary = ["plain", "İstanbul", "a,b", "a;b", 'a"b', "a\tb", "a|b", "two\nlines", ""]
    for delimiter in [",", ";", "\t", "|"]:
        for count in range(1, 21):
            rows = [[randomizer.choice(vocabulary) for _ in range(3)] for _ in range(count)]
            text = io.StringIO(newline="")
            writer = csv.writer(text, delimiter=delimiter)
            writer.writerow(["id", "name", "note"])
            writer.writerows(rows)
            source = text.getvalue().encode("utf-8")
            path = tmp_path / "generated.csv"
            path.write_bytes(source)
            explicit = inspect_file(path, encoding="utf-8", delimiter=delimiter)
            assert explicit.ok, explicit.to_dict()
            assert explicit.records == count
            assert explicit.columns == 3
            assert explicit.encoding.bytes_validated == len(source)
            assert path.read_bytes() == source

            inferred = inspect_file(path, encoding="utf-8")
            if inferred.ok:
                assert inferred.records == count
                assert inferred.columns == 3
                assert inferred.dialect.delimiter == delimiter
            else:
                assert any(issue.code == "dialect_ambiguous" for issue in inferred.issues)


def test_generated_ragged_tails_cannot_hide_behind_a_clean_sample(tmp_path):
    for delimiter in [",", ";", "\t", "|"]:
        text = io.StringIO(newline="")
        writer = csv.writer(text, delimiter=delimiter)
        writer.writerow(["id", "note"])
        writer.writerows([[str(index), "valid"] for index in range(12000)])
        writer.writerow(["missing-column"])
        path = tmp_path / "tail.csv"
        path.write_bytes(text.getvalue().encode("utf-8"))
        result = inspect_file(path, encoding="utf-8")
        assert result.complete and not result.ok
        assert result.records == 12001
        assert result.issues[-1].code == "column_count"
        assert result.issues[-1].record == 12002
        assert result.suggested_read_csv_kwargs is None


@pytest.mark.parametrize("control", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f"])
def test_control_only_lines_are_not_silently_treated_as_blank(tmp_path, control):
    path = tmp_path / "control.csv"
    path.write_bytes(f"id,name\n1,Ada\n{control}\n".encode())
    result = inspect_file(path, encoding="utf-8", delimiter=",")
    assert result.complete and not result.ok
    assert any(
        issue.code == "control_character" and issue.line_start == 3 for issue in result.issues
    )


def test_rejected_directory_always_closes_its_descriptor(tmp_path, monkeypatch):
    from ingest_sentry import inspection

    opened = []
    actual_open = os.open

    def record_open(*args, **kwargs):
        fd = actual_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(inspection.os, "open", record_open)
    with pytest.raises(OSError):
        inspect_file(tmp_path)
    # Windows can reject directories in os.open itself; POSIX opens then rejects.
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO files require POSIX")
def test_fifo_is_rejected_without_blocking_for_a_writer(tmp_path):
    path = tmp_path / "pipe.csv"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        inspect_file(path)
