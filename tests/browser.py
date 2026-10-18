#!/usr/bin/env python3
"""End-to-end test of dm's browser extension in a real Chromium-family browser.

Starts a headless browser with a throwaway profile, the extension loaded and
the native host registered, all pointed at a private dmd (see run.py). Then:

  hand-off   a page sets a login cookie and clicks a download link that only
             works with it: dm must take the download over (cookie included)
             and the browser must drop its copy
  fallback   a one-time link the browser has already used up: dm can't fetch
             it, so the browser must finish the download itself

  python -I tests/browser.py [--browser chromium|brave-origin|...] [--xvfb] [--work DIR]

Headless mode works for Chromium. Brave doesn't open pages when headless, so
--xvfb runs it as a normal window on a private virtual X screen instead; the
browser starts first and the page is then sent to it, as a link opened from
another app would be.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "bin"))
sys.dont_write_bytecode = True

import run  # noqa: E402
from run import Failure, check, expect_done, server, sha_file  # noqa: E402

EXTENSION = os.path.join(ROOT, "extension")
HOST = "io.github.puneethkethanapalli.dm"
EXTENSION_ID = "pmcgbhhhniblapdhpgcjchlgdbjimknf"
SIZE = 4 << 20


def browser_profile(work, config_home, name):
    profile = os.path.join(work, "profile-" + name)
    shutil.rmtree(profile, ignore_errors=True)
    os.makedirs(os.path.join(profile, "Default"))
    # Chromium looks for hosts in the profile it runs; Brave in its default
    # profile folder under XDG_CONFIG_HOME (the test's private one).
    for folder in (profile, os.path.join(config_home, "BraveSoftware", "Brave-Origin"),
                   os.path.join(config_home, "BraveSoftware", "Brave-Browser")):
        os.makedirs(os.path.join(folder, "NativeMessagingHosts"), exist_ok=True)
        with open(os.path.join(folder, "NativeMessagingHosts", HOST + ".json"), "w") as f:
            json.dump({"name": HOST, "description": "dm test", "type": "stdio",
                       "path": os.path.join(ROOT, "bin", "dm-native-host"),
                       "allowed_origins": ["chrome-extension://%s/" % EXTENSION_ID]}, f)
    downloads = os.path.join(work, "browser-downloads-" + name)
    shutil.rmtree(downloads, ignore_errors=True)
    os.makedirs(downloads)
    with open(os.path.join(profile, "Default", "Preferences"), "w") as f:
        json.dump({"download": {"default_directory": downloads, "prompt_for_download": False,
                                "directory_upgrade": True},
                   "brave": {"has_seen_brave_welcome_page": True}}, f)
    # Brave Origin blocks a new profile behind a full-window notice until accepted.
    with open(os.path.join(profile, "Local State"), "w") as f:
        json.dump({"brave": {"origin": {"free_tier_accepted": True}}, "browser": {"first_run_finished": True}}, f)
    return profile, downloads


XVFB = None  # the virtual X server, when --xvfb


def start_xvfb():
    for n in range(90, 120):
        if not os.path.exists("/tmp/.X11-unix/X%d" % n) and not os.path.exists("/tmp/.X%d-lock" % n):
            proc = subprocess.Popen(["Xvfb", ":%d" % n, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
            proc.display = ":%d" % n
            return proc
    sys.exit("no free X display")


def open_page(browser, profile, env, url):
    flags = ["--user-data-dir=" + profile, "--load-extension=" + EXTENSION,
             "--disable-features=DisableLoadExtensionCommandLineSwitch", "--no-first-run",
             "--no-default-browser-check", "--disable-gpu", "--password-store=basic"]
    if not XVFB:
        return subprocess.Popen([browser, "--headless=new"] + flags + [url], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    env = dict(env, DISPLAY=XVFB.display)
    env.pop("WAYLAND_DISPLAY", None)
    flags.append("--ozone-platform=x11")
    proc = subprocess.Popen([browser] + flags, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(5)  # past the welcome page, extension loaded
    # A second launch hands the URL to the running browser and exits.
    subprocess.run([browser] + flags + [url], env=env, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=20)
    return proc


def wait_for(predicate, timeout, message):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.3)
    raise Failure(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", default="chromium")
    parser.add_argument("--work", default=None)
    parser.add_argument("--xvfb", action="store_true", help="run a normal window on a virtual screen")
    args = parser.parse_args()
    global XVFB
    if args.xvfb:
        XVFB = start_xvfb()
    browser = shutil.which(args.browser)
    if not browser:
        sys.exit("%s not found" % args.browser)
    work = args.work or tempfile.mkdtemp(prefix="dm-browser-")
    os.makedirs(work, exist_ok=True)
    srv = server.serve(0)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    inst = run.Instance(work, "browser-e2e", base)
    inst.start()
    failed = 0
    try:
        for name, test in (("hand-off", t_handoff), ("fallback", t_fallback)):
            profile, downloads = browser_profile(work, inst.env["XDG_CONFIG_HOME"], name)
            started = time.time()
            proc = None
            try:
                proc = test(inst, base, browser, profile, downloads)
                print("  ok    %-10s %5.1fs" % (name, time.time() - started))
            except Failure as e:
                failed += 1
                print("  FAIL  %-10s %s" % (name, e))
            finally:
                if proc and proc.poll() is None:
                    os.killpg(proc.pid, 15)
                    proc.wait(10)
    finally:
        inst.stop()
        if XVFB:
            XVFB.terminate()
    print("%d passed, %d failed" % (2 - failed, failed))
    sys.exit(1 if failed else 0)


def t_handoff(inst, base, browser, profile, downloads):
    target = "/login/from-browser.bin?size=%d" % SIZE
    proc = open_page(browser, profile, inst.env, base + "/page?target=" + target.replace("?", "%3F").replace("=", "%3D"))
    job = wait_for(lambda: next((j for j in inst.state()["jobs"] if j["name"] == "from-browser.bin"), None),
                   30, "dm never received the download (is the extension loading?)")
    check(job["source"] == "browser", "job not marked as coming from the browser")
    expect_done(inst, job["id"], "from-browser.bin", SIZE, route_name="from-browser.bin")
    # The browser's copy must be gone, not finished alongside.
    time.sleep(2)
    left = [n for n in os.listdir(downloads) if not n.endswith(".crdownload")]
    check(left == [], "the browser kept its own copy too: %s" % left)
    return proc


def t_fallback(inst, base, browser, profile, downloads):
    target = "/once/single-use.bin?size=%d" % SIZE
    proc = open_page(browser, profile, inst.env, base + "/page?target=" + target.replace("?", "%3F").replace("=", "%3D"))
    path = os.path.join(downloads, "single-use.bin")
    wait_for(lambda: os.path.exists(path), 40, "the browser never finished the download itself")
    check(sha_file(path) == server.expected_sha("single-use.bin", SIZE), "browser's file is damaged")
    check(not any(j["name"] == "single-use.bin" for j in inst.state()["jobs"]),
          "dm kept a job for a download it handed back")
    return proc


if __name__ == "__main__":
    main()
