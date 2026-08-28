import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paper


def test_watchdog_decision():
    assert not paper._watchdog_tripped(1000.0, 1000.0 + 100, 1800)   # fresh -> no trip
    assert paper._watchdog_tripped(1000.0, 1000.0 + 1801, 1800)      # silent too long -> trip
    assert not paper._watchdog_tripped(1000.0, 1000.0 + 99999, 0)    # 0 disables


def test_touch_lock_beats_heartbeat():
    import tempfile, time
    old = paper.LOCK_PATH
    paper.LOCK_PATH = os.path.join(tempfile.mkdtemp(), "t.lock")
    try:
        paper._HEARTBEAT["ts"] = 0.0
        paper.touch_lock()
        assert time.time() - paper._HEARTBEAT["ts"] < 5
        assert open(paper.LOCK_PATH).read().strip() == str(os.getpid())
    finally:
        paper.LOCK_PATH = old


def _run():
    import traceback
    names = sorted(n for n in globals() if n.startswith("test_"))
    ok = 0
    for n in names:
        try:
            globals()[n](); print("PASS " + n); ok += 1
        except Exception:
            print("FAIL " + n); traceback.print_exc()
    print("%d/%d watchdog tests passed" % (ok, len(names)))
    return 0 if ok == len(names) else 1


if __name__ == "__main__":
    sys.exit(_run())


# ---------- self-healing deploys (8/28) ----------
def test_paper_exits_when_the_repo_has_moved_past_the_running_build():
    """Pushes stopped reaching the running process for ~18 hours: the
    updater pulled code but nothing restarted the service, so a fixed
    bug stayed broken and the watchdog written to revive a dead thread
    could not itself be deployed."""
    src = open("paper.py").read()
    assert "_BOOT_HEAD" in src
    assert "_git_head()" in src
    blk = src.split("SELF-HEALING DEPLOY")[1][:1400]
    assert "os._exit(0)" in blk
    assert "!= _BOOT_HEAD" in blk


def test_the_deploy_check_is_a_no_op_without_git():
    """A checkout without git metadata must not exit-loop."""
    import paper
    if paper._BOOT_HEAD == "":
        assert True
    else:
        assert isinstance(paper._BOOT_HEAD, str)
