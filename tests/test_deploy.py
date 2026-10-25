"""Exercise scripts/deploy.sh against a throwaway install with fake venv python/systemctl/curl."""
import os, shutil, sqlite3, subprocess, pytest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(HERE, "scripts", "deploy.sh")
pytestmark = pytest.mark.skipif(not shutil.which("git") or not shutil.which("bash"), reason="needs git and bash")

def sh(*a, cwd=None, env=None, check=True):
    return subprocess.run(a, cwd=cwd, env=env, capture_output=True, text=True, check=check)

def fake(bindir, name, body):
    p = bindir / name; p.write_text("#!/bin/bash\n" + body + "\n"); p.chmod(0o755)

@pytest.fixture
def setup(tmp_path):
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
    src = tmp_path / "src"; src.mkdir()
    sh(*g, "init", "-q", cwd=src)
    (src / "app.txt").write_text("v1\n"); sh("git", "add", ".", cwd=src)
    sh(*g, "commit", "-qm", "v1", cwd=src); sh("git", "tag", "v1.0.0", cwd=src)
    (src / "app.txt").write_text("v2\n"); sh(*g, "commit", "-qam", "v2", cwd=src); sh("git", "tag", "v1.1.0", cwd=src)
    home = tmp_path / "home"; sh("git", "clone", "-q", str(src), str(home))
    sh("git", "checkout", "-q", "v1.0.0", cwd=home)
    (home / "data").mkdir(); db = home / "data" / "backlogr.db"
    sqlite3.connect(db).execute("create table t(x)").connection.commit()
    (home / ".env").write_text(f"BACKLOGR_PORT=9999\n")
    bundle = tmp_path / "b.bundle"; sh("git", "bundle", "create", str(bundle), "--all", "--tags", cwd=src)
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (home / ".venv" / "bin").mkdir(parents=True)
    p = home / ".venv" / "bin" / "python"; p.write_text(f"#!/bin/bash\n[ \"$1\" = -m ] && shift 2\necho pip \"$@\" >> {log}\n"); p.chmod(0o755)
    fake(bin_, "systemctl", f'echo systemctl "$@" >> {log}')
    fake(bin_, "curl", f'echo curl "$@" >> {log}; cat "{tmp_path}/health" 2>/dev/null || printf 200')
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", BACKLOGR_HOME=str(home),
               BACKLOGR_HEALTH_TRIES="2", HOME=str(tmp_path))
    env.pop("BACKLOGR_DB", None); env.pop("BACKLOGR_PORT", None)
    return dict(home=home, bundle=str(bundle), env=env, log=log, tmp=tmp_path)

def run(s, *args):
    return sh("bash", SCRIPT, "--bundle", s["bundle"], *args, env=s["env"], check=False)

def head(s): return (s["home"] / "app.txt").read_text().strip()

def test_dry_run_changes_nothing(setup):
    r = run(setup, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "target: v1.1.0" in r.stdout and "DRY: git checkout" in r.stdout and ":9999" in r.stdout
    assert head(setup) == "v1" and not (setup["home"] / "backups").exists()
    assert not setup["log"].exists() or "systemctl" not in setup["log"].read_text()

def test_deploy_success(setup):
    r = run(setup, "--ref", "v1.1.0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert head(setup) == "v2" and "OK:" in r.stdout
    assert list((setup["home"] / "backups").glob("pre-deploy-*.db"))
    calls = setup["log"].read_text()
    assert "pip install -q -e ." in calls and "systemctl --user restart backlogr" in calls and "127.0.0.1:9999/healthz" in calls

def test_rollback_on_failed_health(setup):
    (setup["tmp"] / "health").write_text("502")
    r = run(setup, "--ref", "v1.1.0")
    assert r.returncode == 1 and "rolling back" in r.stdout
    assert head(setup) == "v1"

def test_rollback_on_failed_pip(setup):
    p = setup["home"] / ".venv" / "bin" / "python"
    p.write_text(f'#!/bin/bash\n[ "$1" = -m ] && shift 2\necho pip "$@" >> {setup["log"]}\ngrep -q v2 app.txt && exit 1; exit 0\n')
    r = run(setup, "--ref", "v1.1.0")
    assert r.returncode == 1 and "pip install FAILED" in r.stdout and "rolled back, healthy" in r.stdout
    assert head(setup) == "v1"

def test_refuses_modified_tracked_files(setup):
    (setup["home"] / "app.txt").write_text("local edit\n")
    r = run(setup, "--ref", "v1.1.0")
    assert r.returncode == 4 and "tracked files modified" in r.stdout
    assert not (setup["home"] / "backups").exists()
