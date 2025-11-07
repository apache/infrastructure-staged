#!/usr/bin/env python3
"""Staging/live web site pubsubber for ASF git repos"""
import asyncio
import configparser
import os
import re
import shutil
import socket
import subprocess
import syslog
import threading
import time

import asfpy.pubsub
import requests

# UUIDs that correspond to our svn repos, for capturing svn pubsub events
SVN_UUIDS = {
    "13f79535-47bb-0310-9956-ffa450edef68": "https://svn-master.apache.org/repos/asf",
    "90ea9780-b833-de11-8433-001ec94261de": "https://svn-master.apache.org/repos/infra",
}

PUBSUB_URL = "https://pubsub.apache.org:2070/"
PUBSUB_QUEUE = {}
GIT_CMD = "/usr/bin/git"
SVN_CMD = "/usr/bin/svn"
SVNWCSUB_CFGFILE = "svnwcsub.conf"
ROOT_DIR = "/www"
BLOGS_ROOT_DIR = "/www/blogs"
CHECKOUT_TIMEOUT = (
    180  # Time out if git operation does not finish within 1 minute (60 seconds)
)
# Staging on staging-vm, publishing on tlp-* boxes
PUBLISH = True if "tlp" in socket.gethostname() else False

# Fastly purge key, only present on tlp-he2
FASTLY_SVC_ID = "4bDOjcgRkWOJy4OpyuG8yT"
FASTLY_BLOGS_SVC_ID = "xkIl7M5JXDA3qxX3KZeze1"
FASTLY_API_KEY = ""
FASTLY_API_FILE = "/home/svnwc/fastly.key"
if os.path.exists(FASTLY_API_FILE):
    FASTLY_API_KEY = open(FASTLY_API_FILE, "r").read()


def purge_site(hostname, svcid=FASTLY_SVC_ID):
    """Purges the newly updated site from Fastly's cache (soft purge)"""
    rv = requests.post(
        "https://api.fastly.com/service/%s/purge/%s" % (svcid, hostname),
        headers={
            "Accept": "application/json",
            "Fastly-Soft-Purge": "1",
            "Fastly-Key": FASTLY_API_KEY,
        },
        timeout=10,
    )
    syslog.syslog(
        syslog.LOG_INFO,
        "Fastly PURGE request for %s responded: %s" % (hostname, rv.text),
    )


def checkout_git_repo(path, source, branch):
    """Checks out a new staging/publish site from a repo"""
    os.chdir(ROOT_DIR)
    # If dir already exists as a dir, delete it first
    if os.path.isdir(path):
        syslog.syslog(syslog.LOG_INFO, "Doing recursive delete of path %s" % path)
        shutil.rmtree(path)
    # Try to pull in the repo, if it fails, just log it for now.
    syslog.syslog(
        syslog.LOG_INFO, "Checking out %s (%s) as %s..." % (source, branch, path)
    )
    try:
        subprocess.check_output(
            (GIT_CMD, "clone", "-b", branch, "--single-branch", source, path),
            timeout=CHECKOUT_TIMEOUT,
        )
        syslog.syslog(syslog.LOG_INFO, "Checkout worked!")
    except subprocess.CalledProcessError as e:
        syslog.syslog(
            syslog.LOG_WARNING, "Could not check out %s: %s" % (source, e.output)
        )
    except subprocess.TimeoutExpired:
        syslog.syslog(
            syslog.LOG_WARNING, "Could not check out %s: operation timed out" % source
        )


def do_git_pull(path, branch):
    """Does a simple git pull (through fetch+reset) from a deploy dir, syslog if it works or not"""
    os.chdir(path)
    # Fetch new changes into .git/ ...
    try:
        subprocess.check_output(
            (GIT_CMD, "fetch", "origin", branch),
            stderr=subprocess.STDOUT,
            timeout=CHECKOUT_TIMEOUT,
        )
        syslog.syslog(
            syslog.LOG_INFO, "Successfully completed `git fetch` into %s" % path
        )
    except subprocess.CalledProcessError as e:
        syslog.syslog(
            syslog.LOG_WARNING,
            "Command `git fetch origin %s` failed in %s: %s" % (branch, path, e.output),
        )
        return
    except subprocess.TimeoutExpired:
        syslog.syslog(
            syslog.LOG_WARNING, "Could not check out %s: operation timed out" % source
        )
        return
    # Merge changes forcibly via git reset --hard origin/$branch.
    # This gets rid of those pesky merge conflicts when someone overrides history.
    try:
        syslog.syslog(
            syslog.LOG_INFO,
            "Refreshing content in %s with: git reset --hard %s" % (path, branch),
        )
        subprocess.check_output(
            (GIT_CMD, "reset", "--hard", "origin/%s" % branch), stderr=subprocess.STDOUT
        )
        syslog.syslog(syslog.LOG_INFO, "%s was successfully updated." % path)
    except subprocess.CalledProcessError as e:
        syslog.syslog(
            syslog.LOG_WARNING,
            "Command `git reset --hard origin/%s` failed in %s: %s"
            % (branch, path, e.output),
        )


def do_svn_up(path):
    """Does a simple svn up from a deploy dir, syslog if it works or not"""
    os.chdir(path)
    try:
        subprocess.check_output(
            (SVN_CMD, "up"),
            stderr=subprocess.STDOUT,
            timeout=CHECKOUT_TIMEOUT,
        )
        syslog.syslog(
            syslog.LOG_INFO, "Successfully completed `svn up` into %s" % path
        )
    except subprocess.CalledProcessError as e:
        syslog.syslog(
            syslog.LOG_WARNING,
            "Command `svn up` failed in %s: %s" % (path, e.output),
            )
        return
    except subprocess.TimeoutExpired:
        syslog.syslog(
            syslog.LOG_WARNING, "Could not run svn up: operation timed out"
        )
        return



def deploy_site(deploydir, source, branch, committer, deploytype="website"):
    """Deploys a git repo to a staging/live site"""

    path = os.path.join(ROOT_DIR, deploydir)

    # Pre-validation:
    if (
        not re.match(r"^[-a-zA-Z0-9/.]+$", deploydir.replace(".apache.org", ""))
    ) or re.search(r"\.\.", deploydir):
        syslog.syslog(syslog.LOG_WARNING, "Invalid deployment dir, %s!" % deploydir)
        return
    if (
        os.path.abspath(path) != path
    ):  # /www/foo.a.o != /www/foo.a.o/../bar.a.o etc, disallow translations.
        syslog.syslog(
            syslog.LOG_WARNING,
            "Invalid deployment dir, %s! (translated path diverges from base web site path)"
            % deploydir,
        )
        return
    if deploytype != "svn" and not source.startswith(
        "https://gitbox.apache.org/repos/asf/"
    ) and not source.startswith("https://github.com/apache/"):
        syslog.syslog(syslog.LOG_WARNING, "Invalid source URL, %s!" % source)
        return
    if not branch:
        syslog.syslog(syslog.LOG_WARNING, "Invalid branch, %s!" % branch)
        return

    # First check if staging dir is already being used.
    # If it is, check if we can just do a git pull...
    if deploytype == "blog":  # blogs have a different root
        path = os.path.join(BLOGS_ROOT_DIR, deploydir.replace(".blog", ""))
    if os.path.isdir(path):
        syslog.syslog(
            syslog.LOG_INFO, "%s is an existing staging dir, checking it..." % path
        )
        # If $path/.svn exists, it's an old svnwcsub checkout, clobber it.
        if os.path.isdir(os.path.join(path, ".svn")):
            if deploytype == "svn":  # we want svn anyway, svn up
                syslog.syslog(
                    syslog.LOG_WARNING,
                    "%s is a subversion directory, running svn up to update" % path,
                    )
                do_svn_up(path)
            else:
                syslog.syslog(
                    syslog.LOG_WARNING,
                    "%s appears to be an old subversion directory, clobbering it!" % path,
                )
                checkout_git_repo(path, source, branch)
            return
        os.chdir(path)
        try:
            csource = (
                subprocess.check_output(
                    (GIT_CMD, "config", "--get", "remote.origin.url")
                )
                .decode("utf-8")
                .strip()
            )
            cbranch = (
                subprocess.check_output((GIT_CMD, "symbolic-ref", "--short", "HEAD"))
                .decode("utf-8")
                .strip()
            )
        except subprocess.CalledProcessError as e:
            syslog.syslog(
                syslog.LOG_WARNING,
                "Could not determine original source of %s, clobbering: %s"
                % (path, e.output),
            )

            checkout_git_repo(path, source, branch)
            return
        # If different repo, clobber dir and re-checkout
        if csource != source:
            syslog.syslog(
                syslog.LOG_INFO,
                "Source repo for %s is not %s (%s), clobbering repo."
                % (path, source, csource),
            )
            checkout_git_repo(path, source, branch)
        # Or if different branch, clobber as well
        elif cbranch != branch:
            syslog.syslog(
                syslog.LOG_INFO,
                "Source branch for %s is not %s (%s), clobbering repo."
                % (path, branch, cbranch),
            )
            checkout_git_repo(path, source, branch)
        # Or it could be all good, just needs a pull
        else:
            syslog.syslog(
                syslog.LOG_INFO, "Source and branch match on-disk, doing git pull"
            )
            do_git_pull(path, branch)
    # Otherwise, do fresh checkout if git, complain (for now) if svn
    else:
        if deploytype == "svn":
            syslog.syslog(
                syslog.LOG_INFO, "%s is a not an existing svn checkout, ignoring payload for now" % path
            )
        else:
            syslog.syslog(
                syslog.LOG_INFO, "%s is a new staging dir, doing fresh checkout" % path
            )
            checkout_git_repo(path, source, branch)


class deploy(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.svnconfig: dict = {}
        if os.path.isfile(SVNWCSUB_CFGFILE):
            self._svnwcsub = configparser.ConfigParser()
            self._svnwcsub.read(SVNWCSUB_CFGFILE)
            self.svnconfig = {
                k: dict(self._svnwcsub.items(k)) for k in self._svnwcsub.sections()
            }

    def run(self):
        """Copy queue, clear it and run each item"""
        global PUBSUB_QUEUE
        while True:
            XPQ = PUBSUB_QUEUE
            PUBSUB_QUEUE = {}
            for deploydir, opts in XPQ.items():
                try:
                    source, branch, committer, hostname, deploytype = opts
                    deploy_site(deploydir, source, branch, committer, deploytype)
                    if PUBLISH and FASTLY_API_KEY:
                        if deploytype == "blog":
                            real_hostname = deploydir.replace(".blog", "")
                            purge_site(
                                f"{real_hostname}.blog.apache.org",
                                svcid=FASTLY_BLOGS_SVC_ID,
                            )  # purge blog.$project.a.o
                        purge_site(hostname)  # purge $project.a.o
                        if hostname == "www.apache.org":
                            purge_site("apache.org")  # Purge both www and non-www here.
                except Exception as e:
                    syslog.syslog(
                        syslog.LOG_WARNING,
                        "BORK: Could not deploy to %s: %s" % (deploydir, e),
                    )
            time.sleep(5)

def common_parent(files: list):
    """Given a list of files changed, figures out the parent (top-most) directory that contains all these commits"""
    top_directory = "/"
    for filename in files:
        if top_directory == "/" or not filename.startswith(top_directory):
            bits = os.path.split(filename)
            for i in range(0, len(bits)-1):
                tmp_path = os.path.join(bits[0], *bits[1:i]) + "/"
                if all(fp.startswith(tmp_path) for fp in files):
                    top_directory = tmp_path
    return top_directory or "/"



async def listen(deployer: deploy):
    """PubSub listener"""
    async for payload in asfpy.pubsub.listen(PUBSUB_URL, timeout=30):
        try:
            expected_action = "staging" if not PUBLISH else "publish"
            # svnpubsub -> publish conversion
            if "commit" in payload and isinstance(payload["commit"], dict) and payload["commit"].get("type", "") == "svn":
                commit = payload["commit"]
                if commit.get("repository", "") in SVN_UUIDS:  # If this is from a repo we know of...
                    svn_uuid = commit["repository"]
                    svn_root = SVN_UUIDS.get(svn_uuid)
                    cp = common_parent(commit.get("changed", []))
                    svn_url = os.path.join(svn_root, cp)
                    print(f"Found commit from {svn_url}...")
                    if deployer.svnconfig:
                        for target, url in deployer.svnconfig.get("track", {}).items():
                            if svn_url.startswith(url) and target.startswith("/"):  #discard config default entries, infra and cms
                                print(f"Found SVN match for {url} in {target}, faking a publish payload")
                                project = "infra"
                                if "/www/" in target:  # Infer project from path if possible
                                    project = os.path.split(target)[1].replace(".apache.org", "")  # /www/commons.apache.org/foo -> commons
                                payload = {
                                    "publish": {
                                        "project": project,
                                        "source": svn_url,
                                        "pusher": commit.get("committer", "root"),
                                        "deploytype": "svn",
                                        "target": target,
                                    }
                                }
            if expected_action in payload:
                project = payload[expected_action].get("project")
                source = payload[expected_action].get("source")
                branch = (
                    payload[expected_action]
                    .get("branch", "asf-site")
                    .replace("refs/heads/", "")
                )
                profile = payload[expected_action].get("profile", "")
                committer = payload[expected_action].get("pusher", "root")
                subdir = payload[expected_action].get("subdir", "")
                deploytype = payload[expected_action].get("type", "website")

                if deploytype not in ("website", "blog", "svn"):
                    deploytype = "website"  # blog or website, nothing in between!

                # Staging dir
                deploydir = project
                if profile:
                    deploydir += "-%s" % profile
                # Or if publishing, use the tlp-server naming format
                if PUBLISH:
                    deploydir = "%s.apache.org" % project
                    # Hardcoded hostnames (aoo etc):
                    if "target" in payload[expected_action]:
                        hostdir = payload[expected_action].get("target")
                        if hostdir:
                            deploydir = hostdir  # Only if not empty value

                if deploydir and source and branch:
                    root_deployment = deploydir  # Logged for purges
                    if subdir and re.match(r"^[-._a-zA-Z0-9/]+$", subdir):
                        syslog.syslog(
                            syslog.LOG_INFO,
                            "Extending deployment [%s] dir %s with subdir %s"
                            % (deploytype, deploydir, subdir),
                        )
                        deploydir = os.path.join(deploydir, subdir)
                    if (
                        deploytype == "blog"
                    ):  # blogs are published under /www/blogs/$project/
                        deploydir = (
                            f"{project}.blog"  # purely for logging/queuing purposes
                        )
                    syslog.syslog(
                        syslog.LOG_INFO,
                        "Found deploy [%s] delivery for %s, deploying as %s"
                        % (deploytype, project, deploydir),
                    )
                    PUBSUB_QUEUE[deploydir] = [
                        source,
                        branch,
                        committer,
                        root_deployment,
                        deploytype,
                    ]

        except ValueError as detail:
            syslog.syslog(
                syslog.LOG_WARNING, f"Bad JSON in payload from {PUBSUB_URL}: {detail}"
            )
            continue
    syslog.syslog(syslog.LOG_WARNING, f"Disconnected from {PUBSUB_URL}, reconnecting")


if __name__ == "__main__":
    deployer = deploy()
    deployer.start()
    asyncio.run(listen(deployer))
