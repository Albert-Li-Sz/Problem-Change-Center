from __future__ import annotations

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from app.public.worker import RunnerOutput  # noqa: E402


def _feed_all(chunks: list[bytes]) -> tuple[str, list[str], int | None]:
    output = RunnerOutput()
    text, events, code = "", [], None
    for chunk in chunks:
        part, new_events, done = output.feed(chunk)
        text += part
        events += new_events
        code = done if done is not None else code
    return text + output.flush(), events, code


def test_multibyte_characters_split_across_reads_stay_intact() -> None:
    data = "读取题目 样例\n".encode()
    # Cut inside a three-byte character.
    text, events, code = _feed_all([data[:4], data[4:]])

    assert text == "读取题目 样例\n"
    assert "\ufffd" not in text
    assert events == [] and code is None


def test_control_lines_are_consumed_instead_of_logged() -> None:
    event = 'P2H_EVENT {"schema_version":1,"phase":"read"}\n'
    stream = f"start\n{event}middle\nP2H_PUBLIC_DONE 3\n".encode()
    # Split inside both markers to exercise buffering of partial lines.
    text, events, code = _feed_all([stream[:10], stream[10:60], stream[60:]])

    assert text == "start\nmiddle\n"
    assert events == [event]
    assert code == 3


def test_output_without_newline_is_shown_and_never_parsed_as_control() -> None:
    output = RunnerOutput()

    assert output.feed(b"working...")[0] == "working..."
    # The rest of that same line only looks like a marker; it is plain text.
    text, events, code = output.feed(b"P2H_PUBLIC_DONE 0\nnext\n")

    assert text == "P2H_PUBLIC_DONE 0\nnext\n"
    assert events == [] and code is None
