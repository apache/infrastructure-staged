#!/usr/bin/env python3
"""Prune tool for staged.apache.org, removing stale checkouts that have been deleted on the remote"""
import os
import subprocess
import shutil

WWW_DIR = "/www"
GIT_CMD = shutil.which("git")

for site_path, subdirs, filenames in os.walk(WWW_DIR):
    # Ensure site_path is a git repo directory, otherwise ignore it
    if ".git" not in subdirs:
        continue
    git_dir = os.path.join(site_path, ".git")
    try:
        # Get current branch
        current_branch = subprocess.check_output(
            (GIT_CMD, "--git-dir", git_dir, "symbolic-ref", "HEAD"), universal_newlines=True, stderr=subprocess.DEVNULL
        ).strip()
        # Check if branch exists on remote
        subprocess.check_output(
            (GIT_CMD, "--git-dir", git_dir, "ls-remote", "--exit-code", "--heads", "origin", current_branch),
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except subprocess.CalledProcessError as e:  # No such branch or repo not found
        if e.returncode == 2:  # No such branch
            print(f"Removing {site_path}, branch no longer present on remote")
            shutil.rmtree(site_path)
        elif e.returncode == 128 and "Repository not found" in e.stderr:  # No such repo??
            print(f"Removing {site_path}, origin repository not found")
            shutil.rmtree(site_path)
