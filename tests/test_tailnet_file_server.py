import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from grix_hermes import tailnet_file_server as fs


def _read(url: str, *, method: str = "GET", data: bytes | None = None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def test_walk_manifest_lists_files_and_empty_dirs(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"0123456789")
    (tmp_path / "empty").mkdir()

    result = fs.walk_manifest(str(tmp_path))
    by_rel = {entry["rel"]: entry for entry in result["entries"]}

    assert result["unreadable"] == 0
    assert by_rel["a.txt"]["size"] == 5
    assert by_rel["a.txt"]["abs"] == str(tmp_path / "a.txt")
    assert by_rel["sub"]["is_dir"] is True
    assert by_rel["sub/b.bin"]["size"] == 10
    assert by_rel["empty"]["is_dir"] is True


def test_file_server_upload_download_and_manifest(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(fs, "is_tailnet_ipv4", lambda _addr: True)
    port = fs.ensure_server_and_get_port("127.0.0.1")
    base = f"http://127.0.0.1:{port}"
    try:
        status, body, _ = _read(f"{base}/ping")
        assert status == 200
        assert body == b"ok"

        file_name = "hello.txt"
        status, body, _ = _read(
            f"{base}/upload?dir={urllib.parse.quote(str(tmp_path))}",
            method="POST",
            data=b"from-phone",
            headers={"X-Filename": urllib.parse.quote(file_name)},
        )
        assert status == 200
        uploaded = json.loads(body)
        assert uploaded["name"] == file_name
        assert (tmp_path / file_name).read_bytes() == b"from-phone"

        status, body, headers = _read(
            f"{base}/download?path={urllib.parse.quote(str(tmp_path / file_name))}"
        )
        assert status == 200
        assert body == b"from-phone"
        assert headers["Content-Length"] == "10"

        (tmp_path / "empty").mkdir()
        status, body, _ = _read(
            f"{base}/manifest?path={urllib.parse.quote(str(tmp_path))}"
        )
        assert status == 200
        manifest = json.loads(body)
        assert manifest["ok"] is True
        rels = {entry["rel"] for entry in manifest["entries"]}
        assert {"hello.txt", "empty"}.issubset(rels)
    finally:
        fs.stop_file_server()
