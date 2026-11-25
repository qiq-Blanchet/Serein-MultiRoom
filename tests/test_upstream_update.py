import importlib.util
import json
from pathlib import Path
import subprocess
import types
from uuid import uuid4
import zipfile

import pytest


def updater():
    path = Path(__file__).parents[1] / 'scripts/upstream_update.py'
    spec = importlib.util.spec_from_file_location('updater', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upstream(module, folder, *, extra=None):
    remote = folder / ('upstream-' + uuid4().hex)
    remote.mkdir()
    files = {name: b'# synthetic source\n' for name in module.REQUIRED}
    # Version metadata deliberately stays the same across code commits.
    files['release-version.json'] = json.dumps({'version':'0.1.0-rc65'}).encode()
    files.update(extra or {})
    files['release-files.json'] = json.dumps(sorted(files)).encode()
    for name, data in files.items():
        path = remote / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    module.git('init', '--quiet', '--template=', '-b', 'main', remote)
    module.git('-C', remote, '-c', 'core.autocrlf=false', 'add', '.')
    commit_source(module, remote)
    module.REMOTE = str(remote)
    return module.latest_commit()


def commit_source(module, remote):
    module.git('-C', remote, '-c', 'user.name=Synthetic Tester', '-c', 'user.email=synthetic@example.invalid',
               'commit', '--quiet', '-m', 'Synthetic source')


@pytest.mark.parametrize('bad', ['../outside', '/absolute', 'C:/escape', 'scripts\\escape',
    'deploy/.env', 'deploy/.env.', 'deploy/runtime/serein.db', '.git/config',
    'deploy/config.toml', 'scripts/CON', 'scripts/x/../../escape', 'scripts/nul.txt'])
def test_rejects_private_and_platform_escape_paths(bad):
    with pytest.raises(ValueError):
        updater().safe_name(bad)


def test_fetches_pinned_commit_when_main_advances(tmp_path):
    module = updater()
    commit = upstream(module, tmp_path, extra={'README.md': b'first source'})
    remote = Path(module.REMOTE)
    (remote / 'README.md').write_bytes(b'second source')
    module.git('-C', remote, 'add', 'README.md'); commit_source(module, remote)
    assert module.latest_commit() != commit
    staged, names = module.prepare(commit, tmp_path)
    assert (staged / 'README.md').read_bytes() == b'first source'
    assert module.git('-C', staged, 'rev-parse', 'HEAD') == commit
    assert module.git('-C', staged, 'rev-list', '--count', 'HEAD') == '1'


@pytest.mark.parametrize('kind', ['missing', 'symlink', 'case_collision', 'private'])
def test_rejects_unsafe_git_source(tmp_path, kind):
    module = updater()
    upstream(module, tmp_path, extra={'README.md': b'synthetic'})
    remote = Path(module.REMOTE)
    names = json.loads((remote / 'release-files.json').read_text())
    name = {'missing':'missing.py', 'symlink':'link', 'case_collision':'readme.md', 'private':'deploy/.env'}[kind]
    names.append(name)
    (remote / 'release-files.json').write_text(json.dumps(names))
    module.git('-C', remote, 'add', 'release-files.json')
    if kind == 'symlink':
        blob = module.git('-C', remote, 'hash-object', '-w', 'README.md')
        module.git('-C', remote, 'update-index', '--add', '--cacheinfo', '120000,'+blob+',link')
    commit_source(module, remote)
    with pytest.raises(ValueError):
        module.prepare(module.latest_commit(), tmp_path)


def test_missing_git_is_actionable_without_touching_install(monkeypatch):
    module = updater()
    def missing(*args, **kwargs):
        raise FileNotFoundError('git')
    monkeypatch.setattr(module.subprocess, 'run', missing)
    with pytest.raises(ValueError, match='安装 Git'):
        module.latest_commit()


def installation(root):
    files = {'release-files.json': b'', 'README.md': b'old source', 'obsolete.py': b'old module'}
    files['release-files.json'] = json.dumps(sorted(files)).encode()
    for name, data in files.items():
        (root / name).write_bytes(data)
    for name in ('deploy/config.toml', 'deploy/.env', 'deploy/runtime/serein.db',
                 'deploy/secrets/api-token', 'deploy/installation.json', 'my-notes.txt'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"backend":"docker"}' if name == 'deploy/installation.json' else b'keep me')
    return files


def test_apply_preserves_state_and_removes_only_obsolete_manifest_files(monkeypatch, tmp_path):
    module = updater()
    root = tmp_path / 'installed'; root.mkdir()
    installation(root)
    stage = tmp_path / 'stage'; stage.mkdir()
    commit = upstream(module, tmp_path, extra={'README.md': b'new source'})
    staged, names = module.prepare(commit, stage)
    all_names, obsolete = module.plan_files(root, names)
    backup = module.backup_sources(root, all_names)
    module.apply_files(root, staged, names, obsolete, backup)
    assert (root / 'README.md').read_bytes() == b'new source'
    assert not (root / 'obsolete.py').exists()
    assert (root / 'deploy/runtime/serein.db').read_bytes() == b'keep me'
    assert (root / 'deploy/config.toml').read_bytes() == b'keep me'
    assert (root / 'deploy/.env').read_bytes() == b'keep me'
    assert (root / 'deploy/secrets/api-token').read_bytes() == b'keep me'
    assert (root / 'my-notes.txt').read_bytes() == b'keep me'
    with zipfile.ZipFile(backup) as archive:
        assert archive.read('README.md') == b'old source'
        assert all(not name.startswith('deploy/') for name in archive.namelist())


def test_failed_file_replacement_restores_all_old_sources(monkeypatch, tmp_path):
    module = updater(); root = tmp_path / 'installed'; root.mkdir()
    files = installation(root)
    stage = tmp_path / 'stage'; stage.mkdir()
    commit = upstream(module, tmp_path, extra={'README.md': b'new source'})
    staged, names = module.prepare(commit, stage)
    all_names, obsolete = module.plan_files(root, names)
    backup = module.backup_sources(root, all_names)
    real = module.replace_file
    calls = 0
    def fail_once(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('disk write failed')
        return real(*args)
    monkeypatch.setattr(module, 'replace_file', fail_once)
    with pytest.raises(OSError):
        module.apply_files(root, staged, names, obsolete, backup)
    assert all((root / name).read_bytes() == data for name, data in files.items())
    assert not (root / 'release-version.json').exists()


@pytest.mark.parametrize('failure', ['', 'download', 'backup', 'build', 'start'])
def test_complete_update_order_and_failure_boundaries(monkeypatch, tmp_path, failure):
    module = updater(); installation(tmp_path)
    commit = upstream(module, tmp_path, extra={'README.md': b'new source'})
    monkeypatch.setattr(module, 'latest_commit', lambda: commit)
    calls = []
    def step(name):
        calls.append(name)
        if name == failure:
            raise OSError(name + ' failed')
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
    manager = types.SimpleNamespace(ROOT=tmp_path, choose=lambda *a, **kw: '1', ensure_tools=lambda: None,
        service_action=lambda action: step('stop'), backup_runtime=lambda: step('backup'), private_file=write)
    fresh = types.SimpleNamespace(ensure_tools=lambda: None, build_runtime=lambda **kw: step('build'),
        service_action=lambda action: step('start' if action == 'up' else 'stop-new'), check_memory=lambda: step('health'))
    monkeypatch.setattr(module, 'load_manager', lambda root: fresh)
    monkeypatch.setattr(module, 'backup_runtime', lambda *a: step('backup'))
    if failure == 'download':
        monkeypatch.setattr(module, 'prepare', lambda *a: step('download'))
    if failure:
        with pytest.raises(OSError):
            module.run_update(manager)
        assert 'health' not in calls
        if failure == 'download':
            assert calls == ['download'] and (tmp_path / 'README.md').read_bytes() == b'old source'
        elif failure == 'backup':
            assert calls == ['stop', 'backup'] and (tmp_path / 'README.md').read_bytes() == b'old source'
        else:
            assert calls[:3] == ['stop', 'backup', 'build']
            if failure == 'start':
                assert calls[-1] == 'stop-new'
    else:
        assert module.run_update(manager)
        assert calls == ['stop', 'backup', 'build', 'start', 'health']
        state = json.loads((tmp_path / 'deploy/update-state.json').read_text())
        assert state['phase'] == 'complete' and state['commit'] == commit and state['branch'] == 'main'
        calls.clear()
        assert not module.run_update(manager) and calls == []
        (tmp_path / 'README.md').write_bytes(b'my local edit')
        with pytest.raises(ValueError, match='被修改'):
            module.run_update(manager)
        assert calls == []
        (tmp_path / 'README.md').write_bytes(b'new source')
        # Another main commit must update even without a release-version bump.
        remote = Path(module.REMOTE)
        (remote / 'README.md').write_bytes(b'next commit source')
        module.git('-C', remote, 'add', 'README.md'); commit_source(module, remote)
        commit = module.git('-C', remote, 'rev-parse', 'HEAD')
        assert module.run_update(manager)
        assert calls == ['stop', 'backup', 'build', 'start', 'health']
        assert (tmp_path / 'README.md').read_bytes() == b'next commit source'
        assert json.loads((tmp_path / 'release-version.json').read_text())['version'] == '0.1.0-rc65'
    assert (tmp_path / 'deploy/runtime/serein.db').read_bytes() == b'keep me'


def test_local_file_collision_stops_before_overwrite(tmp_path):
    module = updater(); installation(tmp_path)
    (tmp_path / 'new.py').write_bytes(b'user file')
    with pytest.raises(ValueError, match='重名'):
        module.plan_files(tmp_path, ['release-files.json', 'new.py'])
    assert (tmp_path / 'new.py').read_bytes() == b'user file'


def test_completed_old_release_does_not_block_main_update(monkeypatch, tmp_path):
    module = updater(); installation(tmp_path)
    commit = upstream(module, tmp_path)
    (tmp_path / 'release-version.json').write_text(json.dumps({'version':'0.1.0-rc65'}))
    (tmp_path / 'deploy/update-state.json').write_text(json.dumps({'version':'v0.1.0-rc65', 'phase':'complete',
                                                                  'source_hashes':module.source_hashes(tmp_path)}))
    choices = []
    manager = types.SimpleNamespace(ROOT=tmp_path, choose=lambda *a, **kw: choices.append(True) or 'r')
    assert not module.run_update(manager)
    assert choices == [True]


def test_real_runtime_backup_while_installer_lock_is_held(tmp_path):
    import tarfile
    from serein.file_lock import exclusive_lock
    module = updater()
    deploy = tmp_path / 'deploy'; (deploy / 'runtime').mkdir(parents=True)
    (deploy / 'runtime/serein.db').write_bytes(b'synthetic-db')
    with exclusive_lock(deploy / 'runtime/installer.lock'):
        backup = module.backup_runtime(Path(__file__).parents[1], deploy)
    with tarfile.open(backup) as archive:
        assert archive.extractfile('runtime/serein.db').read() == b'synthetic-db'
        assert 'runtime/installer.lock' not in archive.getnames()


@pytest.mark.parametrize('selection', ['r', '0'])
def test_cancel_and_skip_data_backup(monkeypatch, tmp_path, selection):
    module = updater(); installation(tmp_path)
    commit = upstream(module, tmp_path)
    monkeypatch.setattr(module, 'latest_commit', lambda: commit)
    calls = []
    manager = types.SimpleNamespace(ROOT=tmp_path, choose=lambda *a, **kw: selection,
        ensure_tools=lambda: None, service_action=lambda action: calls.append(action),
        private_file=lambda path, text: path.write_text(text))
    fresh = types.SimpleNamespace(ensure_tools=lambda: None, build_runtime=lambda **kw: calls.append('build'),
        service_action=lambda action: calls.append(action), check_memory=lambda: None)
    monkeypatch.setattr(module, 'load_manager', lambda root: fresh)
    monkeypatch.setattr(module, 'backup_runtime', lambda *a: pytest.fail('Data backup must be skipped'))
    assert module.run_update(manager) == (selection == '0')
    assert calls == ([] if selection == 'r' else ['stop', 'build', 'up'])


def test_interrupted_same_commit_can_retry_and_hand_edits_are_preserved(monkeypatch, tmp_path):
    module = updater(); installation(tmp_path)
    commit = upstream(module, tmp_path)
    monkeypatch.setattr(module, 'latest_commit', lambda: commit)
    (tmp_path / 'release-version.json').write_text(json.dumps({'version': '0.1.0-rc65'}))
    # A same-commit update must not be mistaken for success after a crash.
    (tmp_path / 'deploy/update-state.json').write_text(json.dumps({'commit': commit, 'phase': 'building'}))
    calls = []
    manager = types.SimpleNamespace(ROOT=tmp_path, choose=lambda *a, **kw: calls.append('offered-retry') or 'r')
    assert not module.run_update(manager)
    assert calls == ['offered-retry']
    hashes = module.source_hashes(tmp_path)
    (tmp_path / 'deploy/update-state.json').write_text(json.dumps({'source_hashes': hashes}))
    (tmp_path / 'README.md').write_text('user edited source')
    with pytest.raises(ValueError, match='被修改'):
        module.plan_files(tmp_path, ['release-files.json', 'README.md'])
    assert (tmp_path / 'README.md').read_text() == 'user edited source'


def test_git_only_files_are_not_copied_into_install(tmp_path):
    module = updater()
    commit = upstream(module, tmp_path)
    remote = Path(module.REMOTE)
    (remote / 'development-only.txt').write_text('not in install manifest')
    module.git('-C', remote, 'add', 'development-only.txt'); commit_source(module, remote)
    staged, names = module.prepare(module.latest_commit(), tmp_path)
    root = tmp_path / 'installed'; root.mkdir(); installation(root)
    all_names, obsolete = module.plan_files(root, names)
    backup = module.backup_sources(root, all_names)
    module.apply_files(root, staged, names, obsolete, backup)
    assert not (root / '.git').exists()
    assert not (root / 'development-only.txt').exists()
    assert (root / 'deploy/runtime/serein.db').read_bytes() == b'keep me'


def test_power_loss_during_replacement_can_resume_without_accepting_other_edits(monkeypatch, tmp_path):
    module = updater(); root = tmp_path / 'installed'; root.mkdir(); installation(root)
    stage = tmp_path / 'stage'; stage.mkdir()
    commit = upstream(module, tmp_path, extra={'README.md': b'new source'})
    staged, names = module.prepare(commit, stage)
    before = module.source_hashes(root)
    (root / 'deploy/update-state.json').write_text(json.dumps({'phase': 'applying', 'source_hashes': before}))
    # Simulate an OS kill after some files, including the manifest, were replaced.
    for name in ('release-files.json', 'release-version.json', 'README.md'):
        (root / name).write_bytes((staged / name).read_bytes())
    all_names, obsolete = module.plan_files(root, names, staged)
    assert 'obsolete.py' in obsolete and 'release-version.json' in all_names
    (root / 'README.md').write_bytes(b'a separate user edit')
    with pytest.raises(ValueError, match='额外修改'):
        module.plan_files(root, names, staged)


def test_bootstrap_uses_existing_instance_and_holds_its_installer_lock(monkeypatch, tmp_path):
    from serein.file_lock import exclusive_lock
    module = updater()
    manager = types.SimpleNamespace(ROOT=tmp_path)
    monkeypatch.setattr(module, 'load_manager', lambda root: manager)
    monkeypatch.setattr(module, 'load_source', lambda path: types.SimpleNamespace(exclusive_lock=exclusive_lock))
    def update(active):
        assert active is manager
        with pytest.raises(RuntimeError, match='已有任务'):
            with exclusive_lock(tmp_path / 'deploy/runtime/installer.lock'):
                pass
    monkeypatch.setattr(module, 'run_update', update)
    assert module.main(['--root', str(tmp_path)]) == 0
    with exclusive_lock(tmp_path / 'deploy/runtime/installer.lock'):
        pass


def test_bootstrap_does_not_update_if_another_installer_is_running(monkeypatch, tmp_path):
    from serein.file_lock import exclusive_lock
    module = updater()
    monkeypatch.setattr(module, 'load_manager', lambda root: types.SimpleNamespace(ROOT=root))
    monkeypatch.setattr(module, 'load_source', lambda path: types.SimpleNamespace(exclusive_lock=exclusive_lock))
    monkeypatch.setattr(module, 'run_update', lambda manager: pytest.fail('Cannot run two installers'))
    with exclusive_lock(tmp_path / 'deploy/runtime/installer.lock'):
        assert module.main(['--root', str(tmp_path)]) == 1


def test_custom_update_source_is_persistent_and_validated(monkeypatch, tmp_path):
    module = updater()
    installation(tmp_path)
    path = tmp_path / 'deploy/installation.json'
    path.write_text(json.dumps({'backend':'docker','compose_project':'same-project'}))
    calls = []
    def git(*args):
        calls.append(args)
        if args[0] == 'ls-remote':
            return 'a' * 40 + '\trefs/heads/feature/cache'
        return ''
    monkeypatch.setattr(module, 'git', git)
    answers = iter(['owner/repo', 'feature/cache'])
    manager = types.SimpleNamespace(ROOT=tmp_path, choose=lambda *a, **k:'1',
        ask=lambda *a:next(answers), confirm=lambda *a:True,
        private_file=lambda p,s:p.write_text(s))
    assert module.configure_source(manager)
    assert module.update_source(tmp_path) == {'repository':'owner/repo','branch':'feature/cache'}
    record = json.loads(path.read_text())
    assert record['compose_project'] == 'same-project'
    assert ('ls-remote','--exit-code','https://github.com/owner/repo.git','refs/heads/feature/cache') in calls
    for repo, branch in [('https://evil.example/x','main'),('owner/repo','-bad'),('owner/..','main')]:
        with pytest.raises(ValueError):
            module.validate_source(repo,branch)


def test_custom_branch_update_uses_same_remote_and_pins_commit(monkeypatch,tmp_path):
    module = updater()
    installation(tmp_path)
    commit = upstream(module,tmp_path,extra={'README.md':b'fork source'})
    local_remote = module.REMOTE
    (tmp_path/'deploy/installation.json').write_text(json.dumps({
        'backend':'docker','update_source':{'repository':'owner/repo','branch':'cache-repair'}}))
    calls=[]
    real_prepare=module.prepare
    def latest(remote,branch):
        calls.append((remote,branch))
        return commit
    def prepare(sha,folder,remote):
        calls.append((sha,remote))
        return real_prepare(sha,folder,local_remote)
    monkeypatch.setattr(module,'latest_commit',latest)
    monkeypatch.setattr(module,'prepare',prepare)
    manager=types.SimpleNamespace(ROOT=tmp_path,choose=lambda *a,**k:'1',ensure_tools=lambda:None,
        service_action=lambda a:None,private_file=lambda p,s:p.write_text(s))
    fresh=types.SimpleNamespace(ensure_tools=lambda:None,build_runtime=lambda **k:None,
        service_action=lambda a:None,check_memory=lambda:None)
    monkeypatch.setattr(module,'load_manager',lambda r:fresh)
    monkeypatch.setattr(module,'backup_runtime',lambda *a:None)
    assert module.run_update(manager)
    assert calls == [('https://github.com/owner/repo.git','cache-repair'),
                     (commit,'https://github.com/owner/repo.git')]
    state=json.loads((tmp_path/'deploy/update-state.json').read_text())
    assert state['repository']=='owner/repo' and state['branch']=='cache-repair'
    assert (tmp_path/'deploy/runtime/serein.db').read_bytes()==b'keep me'
