"""scripts/update.sh against real git repositories and real OpenPGP signatures, with a fake `docker` and `age`."""
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "update.sh"
pytestmark = pytest.mark.skipif(not (shutil.which("gpg") and shutil.which("git") and shutil.which("flock")), reason="needs git, gpg, flock")

DOCKER_STUB = """#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  "compose ps -q app") echo cid123 ;;
  "inspect --format {{.State.Health.Status}} cid123") if [ -e BAD ]; then echo unhealthy; else echo healthy; fi ;;
  "compose exec -T db pg_dump"*) echo DUMPDATA ;;
esac
exit 0
"""
AGE_STUB = "#!/bin/bash\necho \"AGE[$2]\"\ncat\n"


def sh(*cmd, env=None, cwd=None, check=True):
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, check=check)


class Rig:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="tmu"))
        self.origin = self.root / "origin.git"
        self.dev = self.root / "dev"
        self.install = self.root / "install"
        self.bin = self.root / "bin"
        self.log = self.root / "docker.log"
        self.gpg_dev = self._home("gdev")        # the maintainer's own keyring (signs releases)
        self.gpg_trust = self._home("gtrust")    # the server's keyring: release public keys only
        self.gpg_evil = self._home("gevil")      # somebody else's key
        self.fpr = self._genkey(self.gpg_dev)
        self.evil_fpr = self._genkey(self.gpg_evil)
        pub = sh("gpg", "--export", self.fpr, env=self._genv(self.gpg_dev)).stdout if False else subprocess.run(
            ["gpg", "--export", self.fpr], env=self._genv(self.gpg_dev), capture_output=True, check=True).stdout
        subprocess.run(["gpg", "--import"], input=pub, env=self._genv(self.gpg_trust), capture_output=True, check=True)
        self.bin.mkdir()
        for name, body in (("docker", DOCKER_STUB), ("age", AGE_STUB)):
            p = self.bin / name
            p.write_text(body); p.chmod(p.stat().st_mode | stat.S_IEXEC)
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        sh("git", "clone", "-q", str(self.origin), str(self.dev), check=False)
        self.git(self.dev, "checkout", "-q", "-b", "main")
        self.commit("first", {"app.txt": "v1"})
        self.tag("v1.0.0")
        self.git(self.dev, "push", "-q", "origin", "main", "--tags")
        sh("git", "clone", "-q", str(self.origin), str(self.install))
        self.git(self.install, "checkout", "-q", "--detach", "v1.0.0")

    def _home(self, name):
        d = Path(tempfile.mkdtemp(prefix="g")); d.chmod(0o700); return d

    def _genv(self, home):
        return {**os.environ, "GNUPGHOME": str(home)}

    def _genkey(self, home):
        sh("gpg", "--batch", "--passphrase", "", "--quick-gen-key", "Release <r@example.org>", "ed25519", "sign", "never", env=self._genv(home))
        out = sh("gpg", "--list-keys", "--with-colons", env=self._genv(home)).stdout
        return [l.split(":")[9] for l in out.splitlines() if l.startswith("fpr")][0]

    def git(self, cwd, *args, home=None, key=None, check=True):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GNUPGHOME": str(home or self.gpg_dev)}
        base = ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.org", "-c", f"user.signingkey={key or self.fpr}"]
        return sh(*base, *args, env=env, cwd=cwd, check=check)

    def commit(self, msg, files, cwd=None):
        cwd = cwd or self.dev
        for name, content in files.items():
            (Path(cwd) / name).write_text(content)
        self.git(cwd, "add", "-A")
        self.git(cwd, "commit", "-q", "-m", msg)

    def tag(self, name, signed=True, home=None, key=None, cwd=None):
        cwd = cwd or self.dev
        if signed:
            self.git(cwd, "tag", "-s", "-m", name, name, home=home, key=key)
        else:
            self.git(cwd, "tag", name)

    def publish(self, *refs):
        self.git(self.dev, "push", "-q", "-f", "origin", *refs)

    def head(self):
        return self.git(self.install, "rev-parse", "HEAD").stdout.strip()

    def run(self, *args, **env):
        e = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "INSTALL_DIR": str(self.install), "REPO_URL": str(self.origin),
             "TORMAIL_UPDATE_ENV": "/nonexistent", "GNUPGHOME": str(self.gpg_trust), "SKIP_BACKUP": "1", "STUB_LOG": str(self.log),
             "HEALTH_TIMEOUT": "2", "HEALTH_INTERVAL": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run(["bash", str(SCRIPT), *args], env=e, capture_output=True, text=True)

    def docker_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def release(self, version, files, **tagkw):
        self.commit(f"release {version}", files)
        self.tag(version, **tagkw)
        self.publish("main", version)


@pytest.fixture
def rig():
    r = Rig()
    yield r
    shutil.rmtree(r.root, ignore_errors=True)


def test_up_to_date_does_nothing(rig):
    res = rig.run()
    assert res.returncode == 0 and "event=update_none" in res.stdout
    assert not any("build" in c for c in rig.docker_calls())


def test_check_mode_reports_a_verified_update_without_changing_anything(rig):
    before = rig.head()
    rig.release("v1.1.0", {"app.txt": "v2"})
    res = rig.run("--check")
    assert res.returncode == 10 and "event=update_available" in res.stdout and "signature=verified" in res.stdout
    assert "release v1.1.0" in res.stdout and rig.head() == before and not rig.docker_calls()


def test_signed_newer_release_is_installed_rebuilt_and_checked_for_health(rig):
    rig.release("v1.1.0", {"app.txt": "v2"})
    res = rig.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "event=update_done installed=v1.1.0" in res.stdout
    assert (rig.install / "app.txt").read_text() == "v2"
    calls = rig.docker_calls()
    assert any("compose build" in c for c in calls) and any("compose up -d" in c for c in calls)
    assert rig.run().stdout.count("event=update_none") == 1          # and the next run finds nothing to do


def test_newest_tag_wins_not_the_newest_commit(rig):
    rig.release("v1.1.0", {"app.txt": "v2"})
    rig.commit("untagged work in progress", {"app.txt": "wip"}); rig.publish("main")
    assert rig.run().returncode == 0 and (rig.install / "app.txt").read_text() == "v2"


def test_unsigned_release_is_refused(rig):
    before = rig.head()
    rig.release("v1.1.0", {"app.txt": "evil"}, signed=False)         # a lightweight, unsigned tag
    res = rig.run()
    assert res.returncode == 1 and "event=update_refused" in res.stdout and "signature_missing" in res.stdout
    assert rig.head() == before and not any("build" in c for c in rig.docker_calls())


def test_release_signed_by_a_key_we_do_not_trust_is_refused(rig):
    before = rig.head()
    rig.release("v1.1.0", {"app.txt": "evil"}, home=rig.gpg_evil, key=rig.evil_fpr)
    res = rig.run()
    assert res.returncode == 1 and "signature_missing_or_not_from_a_trusted_key" in res.stdout and rig.head() == before


def test_signed_tag_on_rewritten_history_is_refused(rig):
    before = rig.head()
    sh("git", "-C", str(rig.dev), "checkout", "-q", "--orphan", "other")
    rig.commit("unrelated history", {"app.txt": "other"})
    rig.tag("v9.0.0")
    rig.publish("v9.0.0")
    res = rig.run()
    assert res.returncode == 1 and "not_a_fast_forward" in res.stdout and rig.head() == before


def test_older_signed_release_cannot_be_forced_on_us(rig):
    rig.release("v1.1.0", {"app.txt": "v2"})
    assert rig.run().returncode == 0
    new_head = rig.head()
    sh("git", "-C", str(rig.dev), "tag", "-d", "v1.1.0")
    sh("git", "-C", str(rig.origin), "tag", "-d", "v1.1.0")           # the attacker removes the newest tag: only v1.0.0 is left
    res = rig.run()
    assert res.returncode == 1 and "not_a_fast_forward" in res.stdout and rig.head() == new_head


def test_local_modifications_block_the_update(rig):
    rig.release("v1.1.0", {"app.txt": "v2"})
    (rig.install / "app.txt").write_text("hand edited")
    res = rig.run()
    assert res.returncode == 1 and "local_modifications_present" in res.stdout
    assert (rig.install / "app.txt").read_text() == "hand edited"


def test_unhealthy_new_version_is_rolled_back(rig):
    before = rig.head()
    rig.release("v1.1.0", {"app.txt": "v2", "BAD": "this version never becomes healthy"})
    res = rig.run()
    assert res.returncode == 1
    assert "event=update_failed" in res.stdout and "event=rollback_done installed=v1.0.0" in res.stdout and "migrations_are_not_undone" in res.stdout
    assert rig.head() == before and (rig.install / "app.txt").read_text() == "v1"
    assert sum("compose build" in c for c in rig.docker_calls()) == 2


def test_failed_rollback_is_reported_loudly(rig):
    (rig.dev / "BAD").write_text("x"); rig.git(rig.dev, "add", "-A"); rig.git(rig.dev, "commit", "-q", "-m", "bad base")
    rig.tag("v1.0.1"); rig.publish("main", "v1.0.1")
    rig.git(rig.install, "fetch", "-q", "origin", "main", "--tags"); rig.git(rig.install, "checkout", "-q", "--detach", "v1.0.1")
    rig.release("v1.1.0", {"app.txt": "v2"})
    res = rig.run()
    assert res.returncode == 1 and "event=rollback_failed" in res.stdout


def test_backup_is_required_encrypted_and_pruned(rig):
    rig.release("v1.1.0", {"app.txt": "v2"})
    refused = rig.run(SKIP_BACKUP="0")
    assert refused.returncode == 1 and "AGE_RECIPIENT" in refused.stdout and not any("build" in c for c in rig.docker_calls())
    backups = rig.root / "backups"
    backups.mkdir()
    for i in range(5):
        (backups / f"pre-update-2020010{i}T000000Z-old.dump.age").write_text("old")
    res = rig.run(SKIP_BACKUP="0", AGE_RECIPIENT="age1testrecipient", BACKUP_DIR=backups, BACKUP_KEEP="3")
    assert res.returncode == 0, res.stdout + res.stderr
    files = sorted(backups.glob("pre-update-*.dump.age"))
    assert len(files) == 3
    new = [f for f in files if "old" not in f.name][0]
    assert "AGE[age1testrecipient]" in new.read_text() and "DUMPDATA" in new.read_text()
    assert stat.S_IMODE(new.stat().st_mode) == 0o600 and stat.S_IMODE(backups.stat().st_mode) == 0o700


def test_failed_backup_stops_the_update(rig):
    rig.release("v1.1.0", {"app.txt": "v2"})
    res = rig.run(SKIP_BACKUP="0", AGE_RECIPIENT="age1x", BACKUP_CMD="exit 3", BACKUP_DIR=rig.root / "b")
    assert res.returncode == 1 and "backup_failed" in res.stdout and not any("build" in c for c in rig.docker_calls())
    assert list((rig.root / "b").glob("*")) == []


def test_branch_channel_still_requires_a_signed_tip(rig):
    rig.commit("unsigned tip", {"app.txt": "v2"}); rig.publish("main")
    res = rig.run(CHANNEL="branch")
    assert res.returncode == 1 and "signature_missing" in res.stdout


def test_signature_checking_can_be_disabled_only_explicitly_and_says_so(rig):
    rig.commit("unsigned tip", {"app.txt": "v2"}); rig.publish("main")
    res = rig.run(CHANNEL="branch", REQUIRE_SIGNED="0")
    assert res.returncode == 0 and "event=insecure_config" in res.stdout and (rig.install / "app.txt").read_text() == "v2"


def test_missing_keyring_refuses_instead_of_skipping_verification(rig):
    rig.release("v1.1.0", {"app.txt": "v2"})
    res = rig.run(GNUPGHOME=rig.root / "no-such-keyring")
    assert res.returncode == 1 and "no_release_keyring" in res.stdout


def test_unreachable_repository_changes_nothing(rig):
    before = rig.head()
    res = rig.run(REPO_URL=rig.root / "does-not-exist.git")
    assert res.returncode == 1 and "fetch_failed" in res.stdout and rig.head() == before


def test_two_updates_cannot_run_at_once(rig):
    import fcntl
    rig.release("v1.1.0", {"app.txt": "v2"})
    with open(rig.install / ".update.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        res = rig.run()
    assert res.returncode == 0 and "event=update_skipped" in res.stdout and not rig.docker_calls()


def test_update_does_not_touch_the_operators_own_branches_or_tags(rig):
    rig.git(rig.install, "tag", "v1.0.0-local-note")
    rig.git(rig.install, "branch", "mine")
    rig.release("v1.1.0", {"app.txt": "v2"})
    assert rig.run().returncode == 0
    tags = rig.git(rig.install, "tag").stdout.split()
    assert "v1.0.0-local-note" in tags and "v1.1.0" not in tags           # fetched into refs/updater/*, not into your tags
    assert "mine" in rig.git(rig.install, "branch").stdout
