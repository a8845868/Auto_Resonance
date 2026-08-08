import importlib.util
import shutil
import sys
import uuid
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "core" / "model" / "config.py"


def _load_copied_config_module(tmp_path: Path):
    repository = tmp_path / "repository"
    module_path = repository / "core" / "model" / "config.py"
    module_path.parent.mkdir(parents=True)
    shutil.copy2(SOURCE, module_path)

    module_name = f"config_import_side_effect_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module, repository


def test_import_from_foreign_cwd_uses_source_root_without_writing(
    tmp_path, monkeypatch
):
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)
    monkeypatch.delattr(sys, "frozen", raising=False)

    module, repository = _load_copied_config_module(tmp_path)

    assert module.ROOT_PATH == repository.resolve()
    assert module.CONFIG_PATH == repository.resolve() / "config" / "config.json"
    assert module.config.model_dump() == module.Config().model_dump()
    assert not module.CONFIG_PATH.exists()
    assert not (foreign_cwd / "config").exists()


def test_import_reads_existing_config_without_rewriting_it(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    config_path = repository / "config" / "config.json"
    config_path.parent.mkdir(parents=True)
    original_bytes = b'{"version":"9.9.9"}\n'
    config_path.write_bytes(original_bytes)
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)
    monkeypatch.delattr(sys, "frozen", raising=False)

    module, loaded_repository = _load_copied_config_module(tmp_path)

    assert loaded_repository == repository
    assert module.config.version == "9.9.9"
    assert config_path.read_bytes() == original_bytes
    assert not (foreign_cwd / "config").exists()


def test_explicit_save_creates_repository_config_only(tmp_path, monkeypatch):
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)
    monkeypatch.delattr(sys, "frozen", raising=False)
    module, repository = _load_copied_config_module(tmp_path)

    module.config.save_config()

    assert (repository / "config" / "config.json").is_file()
    assert not (foreign_cwd / "config").exists()


def test_frozen_import_uses_executable_directory_without_writing(
    tmp_path, monkeypatch
):
    install_root = tmp_path / "installed-app"
    install_root.mkdir()
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install_root / "gui.exe"))

    module, _repository = _load_copied_config_module(tmp_path)

    assert module.ROOT_PATH == install_root.resolve()
    assert module.CONFIG_PATH == install_root.resolve() / "config" / "config.json"
    assert not module.CONFIG_PATH.exists()
    assert not (foreign_cwd / "config").exists()
