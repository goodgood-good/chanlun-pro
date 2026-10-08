"""Loading the bundled provider must not perform package-update I/O."""
from pathlib import Path
from types import ModuleType, SimpleNamespace
import runpy
import sys

import pytest


@pytest.fixture
def update_probe(monkeypatch):
    calls = []
    requests = ModuleType('requests')
    packages = ModuleType('pkg_resources')

    def distribution(name):
        calls.append(('distribution', name))
        return SimpleNamespace(version='1.0')

    def get(url, **kwargs):
        calls.append(('request', url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {'info': {'version': '1.1'}})

    packages.get_distribution = distribution
    requests.get = get
    monkeypatch.setitem(sys.modules, 'pkg_resources', packages)
    monkeypatch.setitem(sys.modules, 'requests', requests)
    return calls


def load_package():
    path = Path(__file__).resolve().parents[2] / 'src/xtquant/__init__.py'
    return runpy.run_path(str(path), run_name='xtquant-import-probe')


def test_import_never_checks_packages_or_contacts_the_update_service(update_probe, capsys):
    module = load_package()
    assert module['__version__'] == 'xtquant'
    assert update_probe == []
    assert capsys.readouterr().out == ''


def test_update_check_remains_available_when_explicitly_requested(update_probe, capsys):
    module = load_package()
    update_probe.clear()
    capsys.readouterr()
    module['check_for_update']('xtquant')
    assert update_probe == [('distribution', 'xtquant'),
                            ('request', 'https://pypi.org/pypi/xtquant/json', {'timeout': 10})]
    assert 'xtquant1.1' in capsys.readouterr().out
