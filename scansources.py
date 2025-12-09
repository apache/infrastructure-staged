#!/usr/bin/env python3

import os
import subprocess
import json
import time

MAX_NESTING = 4

root_path = "/www/"
svn = "/usr/bin/svn"
git = "/usr/bin/git"
# Has to agree with Pelican build output
output_json = "/www/www.apache.org/output/site-sources.json"
attic_path = "/www/attic.apache.org/projects/%s.html"

def svn_info(path):
    """Fetches svn source URL, revision and change date"""
    try:
        svnroot = (
            subprocess.check_output((svn, "info", "--show-item", "url", path), stderr=subprocess.PIPE,)
            .decode("ascii", "ignore")
            .strip()
        )
    except subprocess.CalledProcessError as e:  # In case of corrupted checkout, bail.
        print(f"Could not determine source of {path}: {e}")
        return None, 0
    svnrevision = (
        subprocess.check_output(
            (svn, "info", "--show-item", "last-changed-revision", path), stderr=subprocess.PIPE,
        )
        .decode("ascii", "ignore")
        .strip()
    )
    svndate = (
        subprocess.check_output((svn, "info", "--show-item", "last-changed-date", path), stderr=subprocess.PIPE,)
        .decode("ascii", "ignore")
        .strip()
    )
    svnversion = svnrevision + " " + svndate
    return svnroot, svnversion


def git_info(path):
    """"Fetches git source URL, branch and revision"""
    dotgitpath = os.path.join(path, ".git")
    gitroot = (
        subprocess.check_output((git, "--git-dir", dotgitpath, "config", "remote.origin.url"), stderr=subprocess.PIPE,)
        .decode("ascii", "ignore")
        .strip()
    )
    gitbranch = (
        subprocess.check_output(
            (git, "--git-dir", dotgitpath, "rev-parse", "--abbrev-ref", "HEAD",), stderr=subprocess.PIPE,
        )
        .decode("ascii", "ignore")
        .strip()
    )
    gitversion = (
        subprocess.check_output(
            (git, "--git-dir", dotgitpath, "show", "--format=%h %ci", "-s", "HEAD",), stderr=subprocess.PIPE,
        )
        .decode("ascii", "ignore")
        .strip()
    )
    return gitroot, gitbranch, gitversion


def get_vcs_type(path: str):
    """Figures out if this path is a git repo, svn repo, both, or none at all"""
    if os.path.exists(os.path.join(path, ".git")):
        if os.path.exists(os.path.join(path, ".svn")):
            return "both"
        return "git"
    elif os.path.exists(os.path.join(path, ".svn")):
        return "svn"


def scan_for_sites(path, publish_settings, childof=None, nest=1):
    for website in os.listdir(path):
        if not childof and ".apache.org" not in website:
            continue
        website_path = os.path.join(path, website)
        if not os.path.isdir(website_path):
            continue

        svnroot = None
        svnversion = None
        gitroot = None
        gitbranch = None
        gitversion = None
        uses_asf_yaml = False

        vcs_type = get_vcs_type(website_path)
        # Is this svn??
        if vcs_type in ("svn", "both"):
            svnroot, svnversion = svn_info(website_path)

        # Is it git??
        if vcs_type in ("git", "both"):
            gitroot, gitbranch, gitversion = git_info(website_path)

            # Check if it's .asf.yaml publishing
            if gitroot and gitbranch:
                yamlpath = os.path.join(website_path, ".asf.yaml")
                if os.path.exists(yamlpath):
                    asfyaml = open(yamlpath, "r").read()
                    uses_asf_yaml = "publish:" in asfyaml.replace("\r", "").split("\n")

        # Log if root web site URL or sub that has a VCS root
        if childof:
            website = childof + "/" + website
        if not childof or (svnroot or gitroot):
            publish_settings[website] = {
                "svn_url": svnroot,
                "svn_version": svnversion,
                "git_url": gitroot,
                "git_branch": gitbranch,
                "git_version": gitversion,
                "asfyaml": uses_asf_yaml,
                "attic": os.path.exists(attic_path % website.split(".")[0]),
                "check_time": int(time.time()),
            }
        if vcs_type or nest < MAX_NESTING:
            scan_for_sites(website_path, publish_settings, childof=website, nest=nest+1)


def main():
    publish_settings = {}
    scan_for_sites(root_path, publish_settings, childof=None, nest=1)
    with open(output_json, "w") as f:
        json.dump(publish_settings, f, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
