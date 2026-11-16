import io
import json
import zipfile

import pytest

from app.artifacts import create_project_zip, parse_project_manifest


def test_create_project_zip_contains_generated_source_and_build_info():
    raw = '{"name":"Demo","files":[{"path":"README.md","content":"# Demo"},{"path":"src/main.py","content":"print(1)"}]}'
    archive, info = create_project_zip(raw)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        assert bundle.read("README.md") == b"# Demo"
        assert bundle.read("src/main.py") == b"print(1)"
        assert b"not a compiled APK" in bundle.read("BUILD_INFO.txt")
    assert info["files"] == 2
    assert info["name"] == "Demo"


@pytest.mark.parametrize("path", ["../outside.txt", "/absolute.txt", "src/../../outside.txt", ".env", "keys/id_rsa"])
def test_rejects_unsafe_project_paths(path):
    raw = '{"files":[{"path":' + json.dumps(path) + ',"content":"no"}]}'
    with pytest.raises(ValueError):
        parse_project_manifest(raw)


def test_rejects_duplicate_paths():
    raw = '{"files":[{"path":"main.py","content":"a"},{"path":"main.py","content":"b"}]}'
    with pytest.raises(ValueError, match="duplicate"):
        parse_project_manifest(raw)


def test_rejects_oversized_manifest():
    raw = '{"files":[{"path":"main.py","content":"' + ("x" * 100001) + '"}]}'
    with pytest.raises(ValueError, match="size limit"):
        parse_project_manifest(raw)
