import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.chatdb import FakeChatDB

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def no_real_resets(tmp_path_factory, monkeypatch):
    """Keeps every test away from the resets written down in data/resets.jsonl."""
    from bench import resets
    monkeypatch.setattr(resets, "RESETS_FILE", tmp_path_factory.mktemp("resets") / "resets.jsonl")


def build_reader(out: Path, *, test: bool) -> Path:
    cmd = [str(ROOT / "reader" / "build.sh"), "--config", str(FIXTURES / "agents.yaml"), "--out", str(out)]
    if test:
        cmd.append("--test")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.fail(f"building bench-reader failed:\n{result.stderr}")
    return out


@pytest.fixture(scope="session")
def reader_bin(tmp_path_factory) -> Path:
    return build_reader(tmp_path_factory.mktemp("bin") / "bench-reader-test", test=True)


class ReaderEnv:
    """A fake chat.db, a fake Messages attachments folder and an inbox folder."""

    def __init__(self, tmp: Path, binary: Path):
        self.binary = binary
        self.db = FakeChatDB(tmp / "chat.db")
        self.attachments = tmp / "Messages" / "Attachments"
        self.attachments.mkdir(parents=True)
        self.out = tmp / "inbox"

    def run(self, decoder: str | None = None) -> list[dict]:
        """Runs one pass of bench-reader and returns the events it wrote."""
        before = len(self.events())
        env = os.environ | ({"BENCH_READER_DECODER": decoder} if decoder else {})
        result = subprocess.run(
            [str(self.binary), "--once", "--db", str(self.db.path),
             "--attachments-root", str(self.attachments), "--out", str(self.out)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, result.stderr
        return self.events()[before:]

    def events(self) -> list[dict]:
        path = self.out / "messages.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def reader(tmp_path, reader_bin) -> ReaderEnv:
    env = ReaderEnv(tmp_path, reader_bin)
    env.run()  # first run records where to start, like a fresh install
    return env
